#!/bin/sh
# Creates the Secret Manager secrets the Cloud Run services read (scripts/gcp-deploy.sh). Random values are made here;
# the others are typed at a hidden prompt (or piped on stdin, one per line, in the order asked). Values go through
# temporary files: never on a command line, never printed. Safe to run again: a secret that exists is never replaced.
#
#   gateway-env          the gateway's own secrets as NAME=value lines, mounted as ONE file (GATEWAY_SECRETS_FILE):
#                        GATEWAY_SECRET_KEY, GATEWAY_ADMIN_KEY, GATEWAY_LLM_API_KEY (the Groq key; optional)
#   woo-mcp-token        woo-mcp's MCP_SERVER_TOKEN; the gateway gets the same secret as WOO_MCP_TOKEN
#   gst-mcp-token        gst's MCP_SERVER_TOKEN; the gateway gets it as GST_MCP_TOKEN
#   woo-consumer-key     the store's WooCommerce REST API key (ck_...): made in the store, asked for here
#   woo-consumer-secret  its secret (cs_...)
#
# Why gateway-env is one secret: Secret Manager bills every secret version that is not destroyed, beyond the first six.
# A secret per value would be 7 now and 9 once the CX and ADK keys exist. A value added to gateway-env later (the LLM
# key skipped at first, say) goes into a NEW version that also holds the old values; the older versions are then
# destroyed. Destroying cannot be undone, which is fine: the new version holds everything they did.
#
# Read the admin key when you need it (this script never prints it):
#   gcloud secrets versions access latest --secret=gateway-env | sed -n 's/^GATEWAY_ADMIN_KEY=//p'
set -eu
. "$(dirname "$0")/lib.sh"
. "$(dirname "$0")/gcp-lib.sh"
need gcloud openssl
PROJECT="$(gcp_project)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; stty echo 2>/dev/null || true' EXIT INT TERM
umask 077

hex() { openssl rand -hex "$1"; }
fernet() { openssl rand -base64 32 | tr '+/' '-_'; }  # a Fernet key: 32 random bytes in URL-safe base64

# ask <prompt>: one line, typed with echo off at a terminal, else read from stdin. Printed on stdout, for $(...).
ask() {
  if [ -t 0 ]; then
    printf '%s (input hidden): ' "$1" >&2
    stty -echo; read -r line || line=""; stty echo; printf '\n' >&2
  else
    read -r line || line=""
  fi
  printf '%s' "$line"
}

# looks_like <what> <value> <extended regex>: like valid(), but never shows the value (it is a secret)
looks_like() {
  printf '%s\n' "$2" | grep -Eqx "$3" || die "that does not look like $1; nothing was created"
}

# create <name> <file>: kept in GCP_REGION only, so each version is billed for one location
create() {
  gcloud secrets create "$1" --project="$PROJECT" --replication-policy=user-managed --locations="$GCP_REGION" \
    --data-file="$2" >/dev/null
  log "created $1"
}

# destroy_older_versions <secret>: keeps only the newest version, since every version not destroyed is billed
destroy_older_versions() {
  versions="$(gcloud secrets versions list "$1" --project="$PROJECT" --filter='NOT state:DESTROYED' --format='value(name)')"
  newest=0
  for v in $versions; do
    v="${v##*/}"
    case "$v" in '' | *[!0-9]*) die "unexpected version '$v' of $1" ;; esac
    if [ "$v" -gt "$newest" ]; then newest="$v"; fi
  done
  for v in $versions; do
    v="${v##*/}"
    if [ "$v" -ne "$newest" ]; then
      gcloud secrets versions destroy "$v" --secret="$1" --project="$PROJECT" --quiet >/dev/null
      log "destroyed old version $v of $1 (version $newest holds all its values)"
    fi
  done
}

gcloud services enable secretmanager.googleapis.com --project="$PROJECT"

# 1. Work out what is missing, and ask for what we cannot make, before changing anything.
if secret_exists gateway-env; then
  gcloud secrets versions access latest --secret=gateway-env --project="$PROJECT" > "$WORK/gateway.env"
  GATEWAY_ENV=kept
else
  { printf 'GATEWAY_SECRET_KEY=%s\n' "$(fernet)"
    printf 'GATEWAY_ADMIN_KEY=wmcp_%s_%s\n' "$(hex 4)" "$(hex 32)"  # the gateway's key format (auth/keys.py)
  } > "$WORK/gateway.env"
  GATEWAY_ENV=new
fi
if ! grep -q '^GATEWAY_LLM_API_KEY=' "$WORK/gateway.env"; then
  llm="$(ask 'Groq API key for the chat agent (Enter to skip: the chat stays off until you run this again)')"
  if [ -n "$llm" ]; then
    looks_like "an API key" "$llm" '[A-Za-z0-9_.-]{8,}'
    printf 'GATEWAY_LLM_API_KEY=%s\n' "$llm" >> "$WORK/gateway.env"
    if [ "$GATEWAY_ENV" = kept ]; then GATEWAY_ENV=changed; fi
  fi
fi
if ! secret_exists woo-consumer-key; then
  ck="$(ask 'WooCommerce consumer key (ck_..., made in the store: WooCommerce > Settings > Advanced > REST API)')"
  looks_like "a WooCommerce consumer key (ck_ and 40 hex characters)" "$ck" 'ck_[0-9a-f]{40}'
  printf '%s' "$ck" > "$WORK/woo-consumer-key"
fi
if ! secret_exists woo-consumer-secret; then
  cs="$(ask 'WooCommerce consumer secret (cs_...)')"
  looks_like "a WooCommerce consumer secret (cs_ and 40 hex characters)" "$cs" 'cs_[0-9a-f]{40}'
  printf '%s' "$cs" > "$WORK/woo-consumer-secret"
fi

# 2. Create what is missing.
case "$GATEWAY_ENV" in
  new) create gateway-env "$WORK/gateway.env" ;;
  changed)
    gcloud secrets versions add gateway-env --project="$PROJECT" --data-file="$WORK/gateway.env" >/dev/null
    log "added GATEWAY_LLM_API_KEY to gateway-env; the gateway reads it at its next start"
    destroy_older_versions gateway-env ;;
  *) log "gateway-env already exists; leaving it alone" ;;
esac
for name in woo-mcp-token gst-mcp-token; do
  if secret_exists "$name"; then
    log "$name already exists; leaving it alone"
  else
    printf '%s' "$(hex 24)" > "$WORK/$name"  # printf '%s': no newline at the end, it would become part of the token
    create "$name" "$WORK/$name"
  fi
done
for name in woo-consumer-key woo-consumer-secret; do
  if [ -f "$WORK/$name" ]; then create "$name" "$WORK/$name"; else log "$name already exists; leaving it alone"; fi
done
log "done. Next: WOO_BASE_URL=https://<store> ADMIN_EMAIL=<you> scripts/gcp-deploy.sh"
