#!/bin/sh
# Runs the tests against the DEPLOYED system (backend/tests/cluster): real pods, Ingress, NetworkPolicies, pod kills.
# Run on the HP after scripts/deploy-local.sh. It changes the cluster on purpose (stops and deletes MCP server pods,
# disables a server for a moment) and puts everything back. Needs uv (the Python runner) and kubectl.
#   scripts/cluster-test.sh            asks for an admin API key (hidden input; it is not stored anywhere)
#   scripts/cluster-test.sh -k gst     extra arguments go to pytest
set -eu
. "$(dirname "$0")/lib.sh"
need uv kubectl
require_context

export CLUSTER_BASE="http://app.$(host_ip).sslip.io" CLUSTER="$CLUSTER" STORE_NS="$STORE_NS" PLATFORM_NS="$PLATFORM_NS"
if [ -z "${CLUSTER_ADMIN_KEY:-}" ]; then
  printf 'Admin API key (input hidden): '
  stty -echo 2>/dev/null || true
  read -r CLUSTER_ADMIN_KEY || CLUSTER_ADMIN_KEY=""
  stty echo 2>/dev/null || true
  printf '\n'
fi
[ -n "$CLUSTER_ADMIN_KEY" ] || die "no admin key given"
export CLUSTER_ADMIN_KEY
log "testing $CLUSTER_BASE"
cd "$ROOT/backend"
exec uv run python -m pytest tests/cluster -v -p no:cacheprovider "$@"
