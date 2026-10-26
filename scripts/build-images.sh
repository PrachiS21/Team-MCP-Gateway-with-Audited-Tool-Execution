#!/bin/sh
# Builds the five images from this checkout and loads them into the cluster (no registry needed locally).
# Build on the machine that runs the cluster: an image built on an arm64 Mac will not run on an amd64 server.
set -eu
. "$(dirname "$0")/lib.sh"
need docker k3d kubectl

# image name  ->  folder with its Dockerfile
for pair in gateway:backend woo-mcp:woo-mcp gst-mcp:upstreams/gst everything-mcp:upstreams/everything dashboard:frontend; do
  log "building ${pair%%:*}:dev"
  docker build -t "${pair%%:*}:dev" "$ROOT/${pair#*:}"
done
log "importing them into cluster '$CLUSTER'"
k3d image import gateway:dev woo-mcp:dev gst-mcp:dev everything-mcp:dev dashboard:dev -c "$CLUSTER"

# The tag is always "dev", so `helm upgrade` sees no change in the pod spec and would leave the old pods running with
# the old image (seen on the HP: a dashboard change did not appear). Restart every deployment that runs one of these
# images, so the freshly imported image is used. Skipped for deployments not installed yet, or another kubectl context.
if [ "$(kubectl config current-context 2>/dev/null || true)" = "k3d-$CLUSTER" ]; then
  for target in "$PLATFORM_NS/gateway" "$STORE_NS/woo-mcp" "$PLATFORM_NS/gst" "$PLATFORM_NS/everything" "$PLATFORM_NS/dashboard"; do
    if kubectl -n "${target%%/*}" get deployment "${target#*/}" >/dev/null 2>&1; then
      kubectl -n "${target%%/*}" rollout restart "deployment/${target#*/}" >/dev/null
      log "restarted $target to use the new image"
    fi
  done
else
  log "kubectl is not pointed at k3d-$CLUSTER: no deployments restarted"
fi
log "done"
