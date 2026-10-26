#!/bin/sh
# Removes the releases AND the namespaces. This deletes the store's database, its files, the audit log and the
# Secrets. It asks first; pass --yes to skip the question. With --cluster the whole k3d cluster is deleted too.
set -eu
. "$(dirname "$0")/lib.sh"
need kubectl helm

YES=0; CLUSTER_TOO=0
for arg in "$@"; do
  case "$arg" in
    --yes) YES=1 ;;
    --cluster) CLUSTER_TOO=1 ;;
    *) die "unknown option $arg (use --yes and/or --cluster)" ;;
  esac
done
require_context

if [ "$YES" -ne 1 ]; then
  printf 'This DELETES namespaces %s and %s with all their data and Secrets. Type "delete" to continue: ' "$STORE_NS" "$PLATFORM_NS"
  read -r answer || answer=""
  [ "$answer" = "delete" ] || die "cancelled; nothing was changed"
fi

helm uninstall platform -n "$PLATFORM_NS" >/dev/null 2>&1 || log "platform release not found (fine)"
helm uninstall store -n "$STORE_NS" >/dev/null 2>&1 || log "store release not found (fine)"
kubectl delete namespace "$PLATFORM_NS" "$STORE_NS" --ignore-not-found --wait
log "namespaces deleted"

if [ "$CLUSTER_TOO" -eq 1 ]; then
  need k3d
  k3d cluster delete "$CLUSTER"
  log "cluster '$CLUSTER' deleted"
fi
