#!/bin/sh
# Installs Tailscale's Kubernetes operator. With the store chart's publicMcp.enabled=true it publishes woo-mcp, and
# only woo-mcp, on the internet through Tailscale Funnel (templates/public-mcp.yaml), for the gateway on Cloud Run.
# Run on the machine with the cluster (the HP).
#
# Once, before running it, in the Tailscale admin console (login.tailscale.com; the free plan is enough):
#   1. DNS: turn on MagicDNS and HTTPS Certificates.
#   2. Access controls: add to the policy file
#        "tagOwners": {"tag:k8s-operator": ["autogroup:admin"], "tag:k8s": ["tag:k8s-operator"]},
#        "nodeAttrs": [{"target": ["tag:k8s"], "attr": ["funnel"]}]
#      Funnel must be granted to tag:k8s by name: the operator's proxies are tagged devices, and the default funnel
#      rule for members does not cover tagged devices.
#   3. Settings > OAuth clients: a new client with read + write on Devices > Core, Keys > Auth Keys and
#      General > Services, and the tag tag:k8s-operator.
#
# This script asks for that client's id and secret at hidden prompts (or reads them from stdin, one per line) and
# stores them as the Secret <namespace>/operator-oauth, which the operator's chart reads when no credentials are given
# to helm. (Giving them to helm would put the secret on a command line.) Then it installs the chart.
# Safe to run again: an existing operator-oauth is kept. Settings: TAILSCALE_NS (default tailscale),
# TAILSCALE_OPERATOR_VERSION (the chart version to pin; default: the newest).
set -eu
. "$(dirname "$0")/lib.sh"
need kubectl helm
require_context
TS_NS="${TAILSCALE_NS:-tailscale}"
VERSION="${TAILSCALE_OPERATOR_VERSION:-}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; stty echo 2>/dev/null || true' EXIT INT TERM
umask 077

kubectl create namespace "$TS_NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
if kubectl -n "$TS_NS" get secret operator-oauth >/dev/null 2>&1; then
  log "$TS_NS/operator-oauth already exists; leaving it alone"
else
  id="$(ask 'Tailscale OAuth client id')"
  looks_like "an OAuth client id" "$id" '[A-Za-z0-9_-]{4,}'
  secret="$(ask 'Tailscale OAuth client secret (tskey-client-...)')"
  looks_like "an OAuth client secret (it starts with tskey-client-)" "$secret" 'tskey-client-[A-Za-z0-9_-]+'
  printf '%s' "$id" > "$WORK/client_id"
  printf '%s' "$secret" > "$WORK/client_secret"
  kubectl -n "$TS_NS" create secret generic operator-oauth \
    --from-file=client_id="$WORK/client_id" --from-file=client_secret="$WORK/client_secret" >/dev/null
  log "created $TS_NS/operator-oauth"
fi

helm repo add tailscale https://pkgs.tailscale.com/helmcharts --force-update >/dev/null
helm repo update tailscale >/dev/null
set --
if [ -n "$VERSION" ]; then set -- --version "$VERSION"; fi
helm upgrade --install tailscale-operator tailscale/tailscale-operator -n "$TS_NS" --wait "$@"
log "operator installed. Next:"
log "  1. set publicMcp.enabled: true in deploy/helm/store/values-local.yaml, then run scripts/deploy-local.sh"
log "  2. kubectl -n $STORE_NS get ingress woo-mcp-public    (after a minute ADDRESS shows woo-mcp.<tailnet>.ts.net)"
log "  3. from any network: curl -s -o /dev/null -w '%{http_code}\\n' https://<that address>/mcp   (401: reachable, token required)"
