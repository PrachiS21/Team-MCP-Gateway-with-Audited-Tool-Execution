#!/bin/sh
# User B's external MCP client for the demo: Claude Code connected to the gateway with ONLY the gateway URL and B's key.
# Runs on the Mac (no kubectl needed).
#
#   scripts/demo-user-b.sh setup     check the gateway answers, ask for B's key (hidden), register it in Claude Code
#   scripts/demo-user-b.sh check     show whether Claude Code is connected
#   scripts/demo-user-b.sh remove    unregister it again (after the demo)
#
# Settings: HOST_IP (the HP's address, default 192.168.1.11), DEMO_DIR (default ~/demo-user-b), CLAUDE_BIN.
# The gateway is registered for DEMO_DIR only ("local" scope), so it never mixes with this project's own Claude Code.
# Claude Code keeps the key in its own settings file (~/.claude.json), outside this project.
set -eu
. "$(dirname "$0")/lib.sh"
need curl

HOST="${HOST_IP:-192.168.1.11}"
printf '%s' "$HOST" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || die "HOST_IP must be an IPv4 address, e.g. 192.168.1.11"
BASE="http://app.$HOST.sslip.io"
DEMO_DIR="${DEMO_DIR:-$HOME/demo-user-b}"

# The claude program: on PATH, or the copy inside the VS Code extension.
CLAUDE="${CLAUDE_BIN:-$(command -v claude || ls -d "$HOME"/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null | tail -1)}"
[ -n "$CLAUDE" ] && [ -x "$CLAUDE" ] || die "Claude Code's 'claude' program was not found; install the CLI or set CLAUDE_BIN"

case "${1:-}" in
  setup)
    # 1. The gateway must answer through the Ingress. /api/me without a login is a 401: that proves the route works.
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$BASE/api/me" || true)"
    [ "$code" = "401" ] || die "the gateway at $BASE did not answer as expected (HTTP ${code:-none}). Is the HP on, and does $BASE open in a browser?"

    # 2. B's key, hidden. It must look like a gateway key: wmcp_<8 hex>_<64 hex>.
    if [ -t 0 ]; then
      printf "User B's API key (input hidden): "
      trap 'stty echo' EXIT INT TERM
      stty -echo
      read -r KEY || KEY=""
      stty echo
      printf '\n'
    else
      read -r KEY || KEY=""
    fi
    printf '%s' "$KEY" | grep -Eq '^wmcp_[0-9a-f]{8}_[0-9a-f]{64}$' \
      || die "that does not look like a gateway API key (wmcp_<8 hex>_<64 hex>); nothing was changed"

    # 3. Register it for DEMO_DIR only. Remove an older registration first, so running setup again replaces the key.
    mkdir -p "$DEMO_DIR"
    cd "$DEMO_DIR"
    "$CLAUDE" mcp remove gateway -s local >/dev/null 2>&1 || true
    "$CLAUDE" mcp add --transport http -s local gateway "$BASE/mcp" --header "Authorization: Bearer $KEY" >/dev/null
    KEY=""
    log "registered 'gateway' -> $BASE/mcp for $DEMO_DIR"

    # 4. Claude Code checks the connection itself.
    "$CLAUDE" mcp list
    cat <<EOF

Next (user B):
  cd $DEMO_DIR && "$CLAUDE"
  /mcp                      -> gateway: the tools of every enabled server (woo__, gst__, everything__)
  What orders came in today in the WooCommerce store?
  How much GST is included in today's WooCommerce orders? Get the order totals from the store, then use the GST calculator.
After the demo:  scripts/demo-user-b.sh remove
EOF
    ;;
  check)
    [ -d "$DEMO_DIR" ] || die "$DEMO_DIR does not exist; run: scripts/demo-user-b.sh setup"
    cd "$DEMO_DIR"
    "$CLAUDE" mcp list
    ;;
  remove)
    [ -d "$DEMO_DIR" ] || { log "nothing to remove ($DEMO_DIR does not exist)"; exit 0; }
    cd "$DEMO_DIR"
    "$CLAUDE" mcp remove gateway -s local >/dev/null 2>&1 && log "removed 'gateway' from Claude Code for $DEMO_DIR" \
      || log "'gateway' was not registered for $DEMO_DIR"
    ;;
  *)
    die "usage: scripts/demo-user-b.sh setup | check | remove"
    ;;
esac
