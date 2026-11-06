#!/bin/sh
# Deletes the Cloud Run services made by gcp-deploy.sh (gateway, woo-mcp, gst, dashboard), and the uptime check of
# gcp-observability.sh, which would only report failures without them. Kept: the secrets, the images, the service
# accounts, the budget alert, the metrics and the dashboard. None of them costs anything inside the free tiers, and
# keeping the secrets means a redeploy brings back the same keys. It asks first; --yes skips the question.
set -eu
. "$(dirname "$0")/lib.sh"
. "$(dirname "$0")/gcp-lib.sh"
need gcloud

YES=0
for arg in "$@"; do
  case "$arg" in
    --yes) YES=1 ;;
    *) die "unknown option $arg (use --yes)" ;;
  esac
done
PROJECT="$(gcp_project)"

if [ "$YES" -ne 1 ]; then
  printf 'This DELETES the Cloud Run services gateway, woo-mcp, gst and dashboard in project %s. Type "delete" to continue: ' "$PROJECT"
  read -r answer || answer=""
  [ "$answer" = "delete" ] || die "cancelled; nothing was changed"
fi

for service in gateway woo-mcp gst dashboard; do
  if gcloud run services describe "$service" --project="$PROJECT" --region="$GCP_REGION" >/dev/null 2>&1; then
    gcloud run services delete "$service" --project="$PROJECT" --region="$GCP_REGION" --quiet
    log "deleted $service"
  else
    log "$service not found (fine)"
  fi
done
checks="$(gcloud monitoring uptime list-configs --project="$PROJECT" --filter='displayName="gateway-health"' \
  --format='value(name)' 2>/dev/null || true)"
for check in $checks; do
  gcloud monitoring uptime delete "${check##*/}" --project="$PROJECT" --quiet >/dev/null
  log "deleted the uptime check gateway-health"
done
log "kept: the secrets, images, service accounts, budget alert, metrics and dashboard (delete them in the console if you want them gone)"
