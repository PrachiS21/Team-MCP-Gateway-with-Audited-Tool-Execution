#!/bin/sh
# Stores the chat agent's LLM API key in platform/gateway-secrets (key LLM_API_KEY) and restarts the gateway.
# The key is typed at a hidden prompt (or piped on stdin). It never appears on a command line, in output or in a file
# you keep. Running it again replaces the key (that is how you rotate it).
#   1. scripts/set-llm-key.sh
#   2. set llm.model in deploy/helm/platform/values-local.yaml, then scripts/deploy-local.sh
set -eu
. "$(dirname "$0")/lib.sh"
need kubectl base64
require_context
kubectl -n "$PLATFORM_NS" get secret gateway-secrets >/dev/null 2>&1 || die "$PLATFORM_NS/gateway-secrets does not exist; run scripts/bootstrap-secrets.sh first"

if [ -t 0 ]; then
  printf 'LLM API key (input hidden): '
  trap 'stty echo' EXIT INT TERM
  stty -echo
  read -r KEY || KEY=""
  stty echo
  printf '\n'
else
  read -r KEY || KEY=""
fi
[ -n "$KEY" ] || die "no key given; nothing was changed"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; stty echo 2>/dev/null || true' EXIT INT TERM
umask 077
printf '{"data":{"LLM_API_KEY":"%s"}}' "$(printf '%s' "$KEY" | base64 | tr -d '\n')" > "$WORK/patch.json"
kubectl -n "$PLATFORM_NS" patch secret gateway-secrets --type merge --patch-file "$WORK/patch.json" >/dev/null
log "stored LLM_API_KEY in $PLATFORM_NS/gateway-secrets"

if kubectl -n "$PLATFORM_NS" get deployment gateway >/dev/null 2>&1; then
  kubectl -n "$PLATFORM_NS" rollout restart deployment/gateway >/dev/null
  log "gateway restarted to read the new key"
fi
log "the chat is on once llm.model is set in deploy/helm/platform/values-local.yaml and scripts/deploy-local.sh has run"
