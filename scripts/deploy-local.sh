#!/bin/sh
# Installs (or upgrades) both charts into the local cluster. The same command works for the first install and for
# every later change. If an UPGRADE fails, Helm rolls back to the previous release; a failed first install is left in place to inspect.
# The store comes first: its setup Job downloads WooCommerce and registers the MCP server's API key.
set -eu
. "$(dirname "$0")/lib.sh"
need docker kubectl helm
require_context

IP="$(host_ip)"
STORE_HOST="shop.$IP.sslip.io"
APP_HOST="app.$IP.sslip.io"

# A node under pressure (or tainted) starts no new pods, so the deploy would just wait until it times out.
# Seen on the HP: after building images the disk was nearly full and the store's setup Job never ran.
pressure="$(kubectl get nodes -o 'jsonpath={.items[*].status.conditions[?(@.status=="True")].type}')"
for p in DiskPressure MemoryPressure PIDPressure; do
  case " $pressure " in *" $p "*) die "the cluster node reports $p, so it will not start new pods. Free space (docker builder prune -f; df -h /) and try again" ;; esac
done
taints="$(kubectl get nodes -o 'jsonpath={.items[*].spec.taints[*].key}')"
[ -z "$taints" ] || die "the cluster node is tainted ($taints), so new pods will not start. See: kubectl describe node | grep -A3 Taints"

for image in gateway:dev woo-mcp:dev gst-mcp:dev everything-mcp:dev dashboard:dev; do
  docker image inspect "$image" >/dev/null 2>&1 || die "image $image is missing; run scripts/build-images.sh first"
done
for s in "$STORE_NS/store-db" "$STORE_NS/store-wordpress" "$STORE_NS/store-mcp" "$PLATFORM_NS/gateway-secrets"; do
  kubectl -n "${s%%/*}" get secret "${s##*/}" >/dev/null 2>&1 || die "Secret $s is missing; run scripts/bootstrap-secrets.sh first"
done

# Roll back automatically only when there is something to roll back TO. On a first install a "rollback" means
# uninstalling, which throws away the pods and logs needed to see what went wrong. Helm 4 renamed --atomic.
rollback_flag() {  # rollback_flag <release> <namespace>
  helm status "$1" -n "$2" >/dev/null 2>&1 || return 0
  case "$(helm version --template '{{.Version}}' 2>/dev/null)" in
    v3.*) printf '%s' --atomic ;;
    *) printf '%s' --rollback-on-failure ;;
  esac
}
hint() { log "failed. The release is left as it is; look with: kubectl -n $1 get pods   and   kubectl -n $1 describe pod <name>"; }

log "store    -> http://$STORE_HOST   (the setup Job takes ~2 min; gives up after 10)"
# shellcheck disable=SC2046
helm upgrade --install store "$ROOT/deploy/helm/store" -n "$STORE_NS" \
  -f "$ROOT/deploy/helm/store/values-local.yaml" --set "host=$STORE_HOST" \
  $(rollback_flag store "$STORE_NS") --wait --wait-for-jobs --timeout 10m || { hint "$STORE_NS"; exit 1; }

log "platform -> http://$APP_HOST  (dashboard; MCP at /mcp)"
# shellcheck disable=SC2046
helm upgrade --install platform "$ROOT/deploy/helm/platform" -n "$PLATFORM_NS" \
  -f "$ROOT/deploy/helm/platform/values-local.yaml" --set "host=$APP_HOST" \
  $(rollback_flag platform "$PLATFORM_NS") --wait --timeout 10m || { hint "$PLATFORM_NS"; exit 1; }

log "done"
log "  storefront : http://$STORE_HOST      admin: http://$STORE_HOST/wp-admin (user admin)"
log "  dashboard  : http://$APP_HOST      (log in with an API key)"
log "  MCP        : http://$APP_HOST/mcp"
log "  first admin API key (printed once at the gateway's first start):"
log "    kubectl -n $PLATFORM_NS logs deploy/gateway | grep -A3 'First admin'"
