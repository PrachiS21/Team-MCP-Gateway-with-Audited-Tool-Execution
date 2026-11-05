#!/bin/sh
# Monitoring for the gateway on Cloud Run, as code, and all inside the free tiers: logs-based metrics counted from the
# gateway's JSON log lines, a dashboard, and an uptime check on /health. Run it after gcp-deploy.sh. Safe to run
# again: metrics are updated from their files; the uptime check and the dashboard are made once.
#
#   deploy/gcp/observability/metrics/*.yaml   one logs-based metric per file (the file name is the metric's name)
#   deploy/gcp/observability/dashboard.json   the "MCP gateway" dashboard
#
# No alert policies, on purpose: they are billed per condition (about $0.10 a month each), so the one notification is
# the budget alert gcp-deploy.sh made. Look at the dashboard, or at the logs (scripts/README.md, "Watching it").
#
# The uptime check calls /health every 15 minutes from Google's checker regions. A side effect worth knowing: the
# gateway then rarely sits idle long enough to stop, so its in-memory database rarely resets. It stays free: 1 million
# checks a month are free and this is a few thousand, and Cloud Run does not bill an instance for waiting.
set -eu
. "$(dirname "$0")/lib.sh"
. "$(dirname "$0")/gcp-lib.sh"
need gcloud
PROJECT="$(gcp_project)"
CONFIG="$ROOT/deploy/gcp/observability"
CHECK="gateway-health"
DASHBOARD="MCP gateway"  # the displayName in dashboard.json

GW_URL="$(gcloud run services describe gateway --project="$PROJECT" --region="$GCP_REGION" \
  --format='value(status.url)' 2>/dev/null || true)"
[ -n "$GW_URL" ] || die "the gateway is not deployed in $PROJECT ($GCP_REGION): run scripts/gcp-deploy.sh first"
valid "the gateway's URL" "$GW_URL" 'https://[a-z0-9.-]+'

gcloud services enable monitoring.googleapis.com logging.googleapis.com --project="$PROJECT"

# 1. Logs-based metrics: Cloud Logging counts matching log lines as they arrive (none are counted backwards in time).
for file in "$CONFIG"/metrics/*.yaml; do
  name="$(basename "$file" .yaml)"
  if gcloud logging metrics describe "$name" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud logging metrics update "$name" --project="$PROJECT" --config-from-file="$file" >/dev/null
    log "updated metric $name"
  else
    gcloud logging metrics create "$name" --project="$PROJECT" --config-from-file="$file" >/dev/null
    log "created metric $name"
  fi
done

# 2. The uptime check (/health, not /healthz: Cloud Run reserves paths ending in "z").
if gcloud monitoring uptime list-configs --project="$PROJECT" --format='value(displayName)' | grep -qxF "$CHECK"; then
  log "uptime check $CHECK already exists"
else
  gcloud monitoring uptime create "$CHECK" --project="$PROJECT" --resource-type=uptime-url \
    --resource-labels="host=${GW_URL#https://},project_id=$PROJECT" --protocol=https --path=/health \
    --period=15 --timeout=10 >/dev/null
  log "created uptime check $CHECK: https://${GW_URL#https://}/health every 15 minutes"
fi

# 3. The dashboard. Made once: to rebuild it from the file, delete it in the console and run this again.
if gcloud monitoring dashboards list --project="$PROJECT" --format='value(displayName)' | grep -qxF "$DASHBOARD"; then
  log "dashboard \"$DASHBOARD\" already exists (delete it in the console and run this again to rebuild it)"
else
  gcloud monitoring dashboards create --project="$PROJECT" --config-from-file="$CONFIG/dashboard.json" >/dev/null
  log "created dashboard \"$DASHBOARD\""
fi
log "done. Dashboard: https://console.cloud.google.com/monitoring/dashboards?project=$PROJECT"
log "metrics count new log lines from now on; reading the logs: scripts/README.md, \"Watching it\""
