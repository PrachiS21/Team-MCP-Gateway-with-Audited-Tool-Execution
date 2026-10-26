#!/bin/sh
# Creates the namespaces and the Secrets the charts refer to, with random values. The values are written straight
# into the cluster: never to a file you keep, never on a command line, never printed.
# Safe to run again: a Secret that already exists is left alone (overwriting a password would lock the database out).
#
#   store-main : store-db, store-wordpress, store-mcp
#   platform   : gateway-secrets   (GATEWAY_SECRET_KEY; WOO_MCP_TOKEN = the SAME token as store-mcp; GST_MCP_TOKEN)
set -eu
. "$(dirname "$0")/lib.sh"
need kubectl openssl base64
require_context

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT INT TERM
umask 077

hex() { openssl rand -hex "$1"; }
# A Fernet key is 32 random bytes in URL-safe base64.
fernet() { openssl rand -base64 32 | tr '+/' '-_'; }
secret_exists() { kubectl -n "$1" get secret "$2" >/dev/null 2>&1; }
secret_value() { kubectl -n "$1" get secret "$2" -o "jsonpath={.data.$3}" | base64 -d; }

# make_secret <namespace> <name> KEY=VALUE...  (values go through temporary files, so they never appear in `ps`)
make_secret() {
  ns="$1"; name="$2"; shift 2
  if secret_exists "$ns" "$name"; then
    log "$ns/$name already exists; leaving it alone"
    return 0
  fi
  rm -rf "$WORK/s"; mkdir "$WORK/s"
  args=""
  for pair in "$@"; do
    key="${pair%%=*}"
    printf '%s' "${pair#*=}" > "$WORK/s/$key"
    args="$args --from-file=$key=$WORK/s/$key"
  done
  # shellcheck disable=SC2086
  kubectl -n "$ns" create secret generic "$name" $args >/dev/null
  log "created $ns/$name"
}

# add_missing_key <namespace> <secret> <KEY> <value>: adds one key to an existing Secret, never changing a key that
# is already there. The value goes through a temporary file, so it never appears on a command line.
add_missing_key() {
  [ -z "$(kubectl -n "$1" get secret "$2" -o "jsonpath={.data.$3}")" ] || return 0
  printf '{"data":{"%s":"%s"}}' "$3" "$(printf '%s' "$4" | base64 | tr -d '\n')" > "$WORK/patch.json"
  kubectl -n "$1" patch secret "$2" --type merge --patch-file "$WORK/patch.json" >/dev/null
  log "added $3 to $1/$2"
}

for ns in "$STORE_NS" "$PLATFORM_NS"; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done

make_secret "$STORE_NS" store-db \
  "DB_PASSWORD=$(hex 16)" "DB_ROOT_PASSWORD=$(hex 16)"
make_secret "$STORE_NS" store-wordpress \
  "WP_ADMIN_PASSWORD=$(hex 12)" "WC_CONSUMER_KEY=ck_$(hex 20)" "WC_CONSUMER_SECRET=cs_$(hex 20)"

# The shared token: reuse the store's if it exists, so the two namespaces always agree.
if secret_exists "$STORE_NS" store-mcp; then
  TOKEN="$(secret_value "$STORE_NS" store-mcp MCP_SERVER_TOKEN)"
else
  TOKEN="$(hex 24)"
fi
make_secret "$STORE_NS" store-mcp "MCP_SERVER_TOKEN=$TOKEN"

if secret_exists "$PLATFORM_NS" gateway-secrets; then
  [ "$(secret_value "$PLATFORM_NS" gateway-secrets WOO_MCP_TOKEN)" = "$TOKEN" ] \
    || die "$PLATFORM_NS/gateway-secrets holds a different token than $STORE_NS/store-mcp. Delete one of them on purpose (and re-run this) so they match; I will not overwrite a Secret."
fi
make_secret "$PLATFORM_NS" gateway-secrets "GATEWAY_SECRET_KEY=$(fernet)" "WOO_MCP_TOKEN=$TOKEN" "GST_MCP_TOKEN=$(hex 24)"
# A Secret made by an older version of this script lacks keys added since: add only what is missing.
add_missing_key "$PLATFORM_NS" gateway-secrets GST_MCP_TOKEN "$(hex 24)"

log "done. Read a value only when you need it, e.g. the WordPress admin password:"
log "  kubectl -n $STORE_NS get secret store-wordpress -o jsonpath='{.data.WP_ADMIN_PASSWORD}' | base64 -d; echo"
log "Back up $PLATFORM_NS/gateway-secrets (GATEWAY_SECRET_KEY): without it the gateway cannot decrypt stored credentials."
