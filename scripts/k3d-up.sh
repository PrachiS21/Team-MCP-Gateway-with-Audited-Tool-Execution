#!/bin/sh
# Creates (or restarts) the local Kubernetes cluster and waits until it can accept the charts.
# Port 80 of this machine goes to the cluster's Traefik, so http://shop.<ip>.sslip.io works from other devices.
set -eu
. "$(dirname "$0")/lib.sh"
need docker k3d kubectl

if k3d cluster list "$CLUSTER" >/dev/null 2>&1; then
  log "cluster '$CLUSTER' exists; making sure it is running"
  k3d cluster start "$CLUSTER" >/dev/null 2>&1 || true
else
  log "creating cluster '$CLUSTER' (one node; ports 80 and 443 published)"
  k3d cluster create "$CLUSTER" --agents 0 -p "80:80@loadbalancer" -p "443:443@loadbalancer" --wait
fi

kubectl config use-context "k3d-$CLUSTER" >/dev/null
log "waiting for the node and for Traefik (the Ingress controller)"
kubectl wait --for=condition=Ready node --all --timeout=180s
# k3s installs Traefik with a separate job a little after the cluster starts, so the Deployment may not exist yet:
# wait for it to appear (up to 5 minutes), then for it to be ready.
tries=0
until kubectl -n kube-system get deployment/traefik >/dev/null 2>&1; do
  tries=$((tries + 1))
  [ "$tries" -le 60 ] || die "Traefik was not installed after 5 minutes; check: kubectl -n kube-system get pods,jobs"
  sleep 5
done
kubectl -n kube-system rollout status deployment/traefik --timeout=300s
log "ready. Next: scripts/build-images.sh, scripts/bootstrap-secrets.sh, scripts/deploy-local.sh"
