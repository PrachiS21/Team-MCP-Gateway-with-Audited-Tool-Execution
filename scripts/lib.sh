# Shared helpers, sourced by the other scripts:  . "$(dirname "$0")/lib.sh"
# Settings (override with environment variables):
#   CLUSTER      k3d cluster name              (default wc)
#   STORE_NS     namespace of the first store  (default store-main)
#   PLATFORM_NS  namespace of the gateway      (default platform)
#   HOST_IP      address people use to reach this machine (default: detected)

CLUSTER="${CLUSTER:-wc}"
STORE_NS="${STORE_NS:-store-main}"
PLATFORM_NS="${PLATFORM_NS:-platform}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

log() { printf '[%s] %s\n' "$(basename "$0" .sh)" "$*"; }
die() { log "error: $*" >&2; exit 1; }

need() {
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "$c is not installed (see scripts/README.md)"
  done
}

# Refuse to touch any cluster except our own local one: a wrong kubectl context is how people delete production.
require_context() {
  current="$(kubectl config current-context 2>/dev/null || true)"
  [ "$current" = "k3d-$CLUSTER" ] || die "kubectl is pointed at '${current:-nothing}', not 'k3d-$CLUSTER'. Run scripts/k3d-up.sh, or: kubectl config use-context k3d-$CLUSTER"
}

# The address of this machine on the LAN, used to build the sslip.io host names (shop.<ip>.sslip.io).
host_ip() {
  addr="${HOST_IP:-}"
  if [ -z "$addr" ]; then
    addr="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}')" || true
  fi
  if [ -z "$addr" ]; then
    addr="$(hostname -I 2>/dev/null | awk '{print $1}')" || true
  fi
  case "$addr" in *[!0-9.]* | "") die "could not work out this machine's IPv4 address; set HOST_IP=192.168.x.y" ;; esac  # also rejects newlines
  printf '%s' "$addr" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || die "could not work out this machine's IPv4 address; set HOST_IP=192.168.x.y"
  printf '%s' "$addr"
}
