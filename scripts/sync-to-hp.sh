#!/bin/sh
# Copies this project to the machine that runs the cluster (from the Mac, to the HP), leaving secrets and local junk behind.
#   scripts/sync-to-hp.sh <user>@192.168.1.11 [destination-folder]      (default folder: ~/wc-platform)
# --delete makes the copy exact, so files removed here are removed there; the destination must be a folder used only for this.
set -eu
. "$(dirname "$0")/lib.sh"
need rsync
[ $# -ge 1 ] || die "usage: scripts/sync-to-hp.sh user@host [destination-folder]"
TARGET="$1"; DEST="${2:-wc-platform}"

rsync -az --delete \
  --exclude '.git/' --exclude '.venv/' --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude 'node_modules/' \
  --exclude '.env' --exclude '.env.*' --exclude '*.pem' --exclude '*.key' --exclude '*.crt' --exclude 'kubeconfig' \
  --exclude '*.db' --exclude '*.db-*' --exclude '*.log' --exclude 'incidents*.jsonl' --exclude '.DS_Store' \
  "$ROOT/" "$TARGET:$DEST/"
log "copied to $TARGET:$DEST"
