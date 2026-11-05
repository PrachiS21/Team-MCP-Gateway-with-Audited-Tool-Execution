#!/bin/sh
# Builds the images with Cloud Build and deploys gst, the gateway and (when asked) woo-mcp to Cloud Run, set up to stay
# inside the free tiers (each step says how). The dashboard joins once it has a Cloud Run proxy config.
# Needs: gcloud, logged in (gcloud auth login); a project with a billing account linked (the free tiers still need
# one); the secrets from scripts/gcp-secrets.sh, made with the same WOO_MCP_URL.
# Safe to run again: each step creates only what is missing, then every service gets a new revision.
#
# Where woo-mcp runs (give exactly one):
#   WOO_MCP_URL=https://woo-mcp.<tailnet>.ts.net/mcp   woo-mcp stays on the HP next to the store, published with
#       Tailscale Funnel (store chart: publicMcp). Nothing is built or deployed for it; the gateway's seed points there.
#       The store's API and its WooCommerce keys never leave the HP.
#   WOO_BASE_URL=https://shop.example.com              woo-mcp runs on Cloud Run and calls a store that has its own
#       public HTTPS address (a VPS, say).
#
#   WOO_MCP_URL=https://woo-mcp.<tailnet>.ts.net/mcp ADMIN_EMAIL=you@example.com scripts/gcp-deploy.sh
#
# Other settings: ADMIN_EMAIL (required); ADK_AGENT_EMAIL and CX_AGENT_EMAIL (the members whose keys are ADK_AGENT_KEY
# and CX_AGENT_KEY in gateway-env; default adk-agent@ / cx-agent@example.com); CX_TOOLS (optional: the tools the
# CX webhook may use, comma-separated <server>__<tool>; read-only ones only, whatever is listed); LLM_MODEL (default openai/gpt-oss-120b, as values-local.yaml);
# STORE_TIMEZONE (default Asia/Kolkata); STORE_STATE (gst's home state, default MH); BUDGET (alert amount in the
# billing account's currency, default 1); GCP_PROJECT, GCP_REGION, GCP_REPO (gcp-lib.sh); GCP_TAG (image tag).
#
# The price of free is scale-to-zero (the Kubernetes deployment keeps all of this):
#   - after about 15 idle minutes the gateway stops, and its in-memory disk goes with it: the dashboard's audit view,
#     sessions and members invited from the dashboard reset. The admin key, the servers and seeded users come back
#     by themselves (GATEWAY_ADMIN_KEY and the seed), and every audit line is kept in Cloud Logging.
#   - the first request after an idle spell waits a few seconds while the services start.
#   - the undo worker gets CPU only while a request is being served.
set -eu
. "$(dirname "$0")/lib.sh"
. "$(dirname "$0")/gcp-lib.sh"
need gcloud git
PROJECT="$(gcp_project)"

WOO_BASE_URL="${WOO_BASE_URL:-}"
ADMIN_EMAIL="${ADMIN_EMAIL:-}"
ADK_AGENT_EMAIL="${ADK_AGENT_EMAIL:-adk-agent@example.com}"
CX_AGENT_EMAIL="${CX_AGENT_EMAIL:-cx-agent@example.com}"
CX_TOOLS="${CX_TOOLS:-}"
LLM_MODEL="${LLM_MODEL:-openai/gpt-oss-120b}"
STORE_TIMEZONE="${STORE_TIMEZONE:-Asia/Kolkata}"
STORE_STATE="${STORE_STATE:-MH}"
BUDGET="${BUDGET:-1}"

# -- 0. Check every setting before changing anything. None of them may hold a comma or a quote: they end up in
#       gcloud's comma-separated lists and in single-quoted YAML.
if [ -n "$WOO_MCP_URL" ] && [ -n "$WOO_BASE_URL" ]; then
  die "set WOO_MCP_URL (woo-mcp on the HP) or WOO_BASE_URL (woo-mcp on Cloud Run), not both"
elif [ -n "$WOO_MCP_URL" ]; then
  case "$WOO_MCP_URL" in
    https://*) ;;
    *) die "WOO_MCP_URL must start with https://: the gateway sends woo-mcp's token with every request" ;;
  esac
  valid WOO_MCP_URL "$WOO_MCP_URL" 'https://[A-Za-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9._~/-]*'
  WOO_ON_CLOUD_RUN=0
elif [ -n "$WOO_BASE_URL" ]; then
  case "$WOO_BASE_URL" in
    https://*) ;;
    *) die "WOO_BASE_URL must start with https://: woo-mcp sends the store's API key with every request" ;;
  esac
  valid WOO_BASE_URL "$WOO_BASE_URL" 'https://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?'
  WOO_ON_CLOUD_RUN=1
else
  die "say where woo-mcp runs: WOO_MCP_URL=https://woo-mcp.<tailnet>.ts.net/mcp (on the HP) or WOO_BASE_URL=https://<store> (woo-mcp on Cloud Run)"
fi
[ -n "$ADMIN_EMAIL" ] || die "set ADMIN_EMAIL to the first admin's email (their key is GATEWAY_ADMIN_KEY in gateway-env)"
valid ADMIN_EMAIL "$ADMIN_EMAIL" '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]+'
valid ADK_AGENT_EMAIL "$ADK_AGENT_EMAIL" '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]+'
valid CX_AGENT_EMAIL "$CX_AGENT_EMAIL" '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]+'
valid CX_TOOLS "$CX_TOOLS" '([A-Za-z0-9_-]+(,[A-Za-z0-9_-]+)*)?'
valid LLM_MODEL "$LLM_MODEL" '[A-Za-z0-9._/:-]+'
valid STORE_TIMEZONE "$STORE_TIMEZONE" '[A-Za-z0-9_/+-]+'
valid STORE_STATE "$STORE_STATE" '[A-Z]{2}'
valid BUDGET "$BUDGET" '[0-9]+(\.[0-9]+)?'
if [ -n "${GCP_TAG:-}" ]; then
  TAG="$GCP_TAG"
else  # a new tag every time, so every deploy is a revision you can tell apart (and roll back to)
  TAG="$(date -u +%Y%m%d-%H%M%S)-$(git -C "$ROOT" rev-parse --short HEAD)"
  if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then TAG="$TAG-dirty"; fi  # built from uncommitted changes
fi
valid GCP_TAG "$TAG" '[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}'

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT INT TERM
umask 077

# -- 1. Billing. Cloud Run needs a billing account even inside the free tier.
ACCOUNT="$(gcloud billing projects describe "$PROJECT" --format='value(billingAccountName)' 2>/dev/null || true)"
[ -n "$ACCOUNT" ] || die "project $PROJECT has no billing account linked (or you cannot see it). Link one in the console under Billing: the free tiers still need one"

log "enabling the APIs (quick when they are already on)"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com iam.googleapis.com billingbudgets.googleapis.com --project="$PROJECT"

NEEDED="$GCP_SECRETS"
if [ "$WOO_ON_CLOUD_RUN" = 1 ]; then NEEDED="$NEEDED $GCP_STORE_SECRETS"; fi
for s in $NEEDED; do
  secret_exists "$s" || die "secret $s does not exist: run scripts/gcp-secrets.sh first (with the same WOO_MCP_URL)"
done
# The seed below names these keys; without one the gateway would refuse to start. The values go straight into grep.
for name in ADK_AGENT_KEY CX_AGENT_KEY; do
  gcloud secrets versions access latest --secret=gateway-env --project="$PROJECT" | grep -q "^$name=" \
    || die "gateway-env has no $name yet: run scripts/gcp-secrets.sh again (it adds it and keeps the rest)"
done

# A budget alert: billing admins get an email at 50% and 100% of BUDGET. It only WARNS, it never stops spending;
# with the default of 1 (in the billing account's currency) any real charge sets it off. Needs the Billing Account
# Administrator role, so a failure here is a warning, not a stop.
BUDGET_NAME="$PROJECT free-tier guard"
ensure_budget() {
  names="$(gcloud billing budgets list --billing-account="$ACCOUNT" --billing-project="$PROJECT" \
    --format='value(displayName)')" || return 1
  if printf '%s\n' "$names" | grep -qxF "$BUDGET_NAME"; then
    log "budget alert '$BUDGET_NAME' already exists"
    return 0
  fi
  gcloud billing budgets create --billing-account="$ACCOUNT" --billing-project="$PROJECT" \
    --display-name="$BUDGET_NAME" --budget-amount="$BUDGET" --filter-projects="projects/$PROJECT" \
    --threshold-rule=percent=0.5 --threshold-rule=percent=1.0 >/dev/null || return 1
  log "budget alert '$BUDGET_NAME' created: an email at 50% and 100% of $BUDGET (billing account currency)"
}
if ! ensure_budget; then
  log "warning: could not set up the budget alert. Make one in the console (Billing > Budgets & alerts)" >&2
fi

# -- 2. Image repository. Free up to 0.5 GB: the cleanup policy (deploy/gcp/cleanup-policy.json) keeps the two newest
#       versions of each image, so a rollback has something to go back to, and deletes the rest. --no-dry-run makes
#       the deleting real. Google applies it about once a day.
if ! gcloud artifacts repositories describe "$GCP_REPO" --location="$GCP_REGION" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$GCP_REPO" --repository-format=docker --location="$GCP_REGION" --project="$PROJECT"
  log "created image repository $GCP_REPO"
fi
gcloud artifacts repositories set-cleanup-policies "$GCP_REPO" --location="$GCP_REGION" --project="$PROJECT" \
  --policy="$ROOT/deploy/gcp/cleanup-policy.json" --no-dry-run --quiet >/dev/null

# -- 3. Where Cloud Build's source upload goes. Its default bucket is multi-region, which the Cloud Storage free tier
#       does not cover, and it keeps every upload forever. This one is regional and deletes uploads after a day
#       (deploy/gcp/build-source-lifecycle.json).
BUCKET="gs://$PROJECT-build-source"
if ! gcloud storage buckets describe "$BUCKET" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud storage buckets create "$BUCKET" --project="$PROJECT" --location="$GCP_REGION" --uniform-bucket-level-access
  log "created $BUCKET"
fi
gcloud storage buckets update "$BUCKET" --project="$PROJECT" \
  --lifecycle-file="$ROOT/deploy/gcp/build-source-lifecycle.json" >/dev/null

# -- 4. One service account per service, allowed to read only its own secrets, with no project roles at all. (Cloud
#       Run's default account is a project Editor: a bug in any service could then change the whole project.)
retry() { n=1; until "$@"; do [ "$n" -lt 5 ] || return 1; n=$((n + 1)); sleep 5; done; }
service_account() {
  name="$1-run"; shift
  email="$name@$PROJECT.iam.gserviceaccount.com"
  if ! gcloud iam service-accounts describe "$email" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$name" --project="$PROJECT" --display-name="Cloud Run: ${name%-run}" >/dev/null
    log "created service account $name"
  fi
  for secret in "$@"; do  # retried: a new account takes a few seconds to become usable in IAM
    retry gcloud secrets add-iam-policy-binding "$secret" --project="$PROJECT" --member="serviceAccount:$email" \
      --role=roles/secretmanager.secretAccessor >/dev/null
  done
}
service_account gateway gateway-env woo-mcp-token gst-mcp-token
service_account gst gst-mcp-token
if [ "$WOO_ON_CLOUD_RUN" = 1 ]; then service_account woo-mcp woo-mcp-token $GCP_STORE_SECRETS; fi

# -- 5. Build the images (deploy/gcp/cloudbuild.yaml; woo-mcp's own file only when it runs on Cloud Run).
#       .gcloudignore keeps secrets and local files out of the upload. The free build minutes are for the default
#       machine, which the configs keep.
REPO_URL="$GCP_REGION-docker.pkg.dev/$PROJECT/$GCP_REPO"
build() {
  gcloud builds submit "$ROOT" --project="$PROJECT" --config="$ROOT/deploy/gcp/$1" \
    --substitutions="_REPO=$REPO_URL,_TAG=$TAG" --gcs-source-staging-dir="$BUCKET/source"
}
log "building gateway and gst-mcp as :$TAG with Cloud Build (a few minutes)"
build cloudbuild.yaml
if [ "$WOO_ON_CLOUD_RUN" = 1 ]; then
  log "building woo-mcp as :$TAG"
  build cloudbuild-woo-mcp.yaml
fi

# -- 6. Deploy. Every service: no instance while idle (nothing billed); CPU only while a request is served
#       (--cpu-throttling, the billing the free tier is for); a cap on instances, so a flood of requests cannot run up
#       a bill; its own service account. Cloud Run sets PORT=8080 and the images read it.
deploy() {
  service="$1"; image="$2"; shift 2
  gcloud run deploy "$service" --project="$PROJECT" --region="$GCP_REGION" --image="$REPO_URL/$image:$TAG" \
    --service-account="$service-run@$PROJECT.iam.gserviceaccount.com" --allow-unauthenticated \
    --min-instances=0 --cpu-throttling --cpu=1 --memory=512Mi --port=8080 --quiet "$@"
}
url_of() { gcloud run services describe "$1" --project="$PROJECT" --region="$GCP_REGION" --format='value(status.url)'; }

# The upstream servers have public URLs (the gateway reaches them over the internet) and refuse any request without
# their MCP_SERVER_TOKEN.
if [ "$WOO_ON_CLOUD_RUN" = 1 ]; then
  deploy woo-mcp woo-mcp --max-instances=2 --timeout=60 \
    --set-env-vars="WOO_BASE_URL=$WOO_BASE_URL,STORE_TIMEZONE=$STORE_TIMEZONE" \
    --set-secrets="MCP_SERVER_TOKEN=woo-mcp-token:latest,WOO_CONSUMER_KEY=woo-consumer-key:latest,WOO_CONSUMER_SECRET=woo-consumer-secret:latest"
  WOO_URL="$(url_of woo-mcp)"
  valid "woo-mcp's URL" "$WOO_URL" 'https://[a-z0-9.-]+'
  WOO_MCP_URL="$WOO_URL/mcp"
fi
deploy gst gst-mcp --max-instances=2 --timeout=60 \
  --set-env-vars="STORE_STATE=$STORE_STATE" --set-secrets="MCP_SERVER_TOKEN=gst-mcp-token:latest"
GST_URL="$(url_of gst)"
valid "gst's URL" "$GST_URL" 'https://[a-z0-9.-]+'

# The seed names the two servers (woo-mcp where it runs, gst at its Cloud Run URL) and the member users of the ADK
# agent and the CX webhook, whose keys must survive the database being wiped when the gateway idles. It holds no secrets (the gateway refuses a
# seed that does), only the names of the variables holding them, so it can be plain config.
server() { printf '{"slug": "%s", "name": "%s", "url": "%s", "credential_type": "bearer", "credential_env": "%s"}' "$@"; }
member() { printf '{"email": "%s", "role": "member", "key_env": "%s"}' "$@"; }
SEED="$(printf '{"servers": [%s, %s], "users": [%s, %s]}' \
  "$(server woo 'WooCommerce (main)' "$WOO_MCP_URL" WOO_MCP_TOKEN)" "$(server gst 'GST calculator' "$GST_URL/mcp" GST_MCP_TOKEN)" \
  "$(member "$ADK_AGENT_EMAIL" ADK_AGENT_KEY)" "$(member "$CX_AGENT_EMAIL" CX_AGENT_KEY)")"

# A YAML file rather than --set-env-vars, whose commas would split the JSON. Every value was checked above and none
# can hold a single quote, so single-quoted YAML is safe.
cat > "$WORK/gateway-vars.yaml" <<EOF
GATEWAY_ADMIN_EMAIL: '$ADMIN_EMAIL'
GATEWAY_SECRETS_FILE: /secrets/gateway/env
GATEWAY_SEED_JSON: '$SEED'
GATEWAY_LLM_MODEL: '$LLM_MODEL'
GATEWAY_LOG_FORMAT: json
GATEWAY_GCP_PROJECT: '$PROJECT'
GATEWAY_COOKIE_SECURE: '1'
GATEWAY_CX_ENABLED: '1'
EOF
if [ -n "$CX_TOOLS" ]; then printf "GATEWAY_CX_TOOLS: '%s'\n" "$CX_TOOLS" >> "$WORK/gateway-vars.yaml"; fi

# The gateway: at most ONE instance. Each instance has its own in-memory database, so a second one would be a second,
# different database (log in on one, look logged out on the next request). gateway-env is mounted as a file, the two
# upstream tokens are variables, and it cannot read the store's WooCommerce keys (with woo-mcp on Cloud Run only
# woo-mcp can; on the HP they are not in Google Cloud at all). It is ready when /ready answers (the database is up;
# the seed was applied before the port opened).
deploy gateway gateway --max-instances=1 --timeout=300 \
  --startup-probe=httpGet.path=/ready,httpGet.port=8080,periodSeconds=2,timeoutSeconds=2,failureThreshold=30 \
  --env-vars-file="$WORK/gateway-vars.yaml" \
  --set-secrets="/secrets/gateway/env=gateway-env:latest,WOO_MCP_TOKEN=woo-mcp-token:latest,GST_MCP_TOKEN=gst-mcp-token:latest"
GW_URL="$(url_of gateway)"

log "done. Gateway: $GW_URL   (MCP endpoint $GW_URL/mcp, health $GW_URL/health); woo-mcp: $WOO_MCP_URL"
log "the admin key, when you need it: gcloud secrets versions access latest --secret=gateway-env --project=$PROJECT | sed -n 's/^GATEWAY_ADMIN_KEY=//p'"
log "Dialogflow CX webhook: $GW_URL/api/cx/webhook with the header Authorization: Bearer <CX_AGENT_KEY> (read it the same way)"
log "Claude Code: claude mcp add --transport http gateway $GW_URL/mcp --header \"Authorization: Bearer <the admin key>\""
log "after an idle spell the first request takes a few seconds: the services start from zero"
