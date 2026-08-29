#!/usr/bin/env bash
# Bring up Tailscale inside a Claude cloud-session container so scripts here
# can reach TWS on the owner's machine. Full runbook: docs/tailscale.md
# (environment network policy, Windows-side setup, security posture).
#
# Usage:
#   bash scripts/tailscale_up.sh          # install if needed, join tailnet
#   bash scripts/tailscale_up.sh status   # show daemon/tailnet state only
#
# Auth: set TS_AUTHKEY (ideally as an environment variable in the Claude
# environment config — use an EPHEMERAL, reusable, tagged key) to join
# non-interactively. Without it, the script prints a login URL to click.
#
# The container is ephemeral: run once per session. Idempotent — re-running
# skips whatever is already done.
set -euo pipefail

SOCKS_PORT="${TS_SOCKS_PORT:-1055}"
HTTP_PROXY_PORT="${TS_HTTP_PROXY_PORT:-1056}"
STATE_DIR="${TS_STATE_DIR:-/tmp/tailscale-state}"
SOCKET="/tmp/tailscaled.sock"
LOG="/tmp/tailscaled.log"
NODE_NAME="${TS_HOSTNAME:-medik-cloud}"

ts() { tailscale --socket="$SOCKET" "$@"; }
say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$1" >&2; exit "${2:-1}"; }

backend_state() {
    ts status --json 2>/dev/null | python3 -c \
        'import json,sys;print(json.load(sys.stdin).get("BackendState",""))' 2>/dev/null || true
}

if [[ "${1:-}" == "status" ]]; then
    if [[ -S "$SOCKET" ]]; then
        ts status || true
        say "SOCKS5 proxy: 127.0.0.1:$SOCKS_PORT"
    else
        say "tailscaled is not running (no socket at $SOCKET)."
    fi
    exit 0
fi

# ---- 1. Binaries ------------------------------------------------------------
# pkgs.tailscale.com is not on the environment allowlist, but proxy.golang.org
# is, so the official client builds from source through the Go module proxy.
if ! command -v tailscaled >/dev/null 2>&1 || ! command -v tailscale >/dev/null 2>&1; then
    command -v go >/dev/null 2>&1 || die "Go toolchain not found; cannot build tailscale."
    if [[ -w /usr/local/bin ]]; then GOBIN=/usr/local/bin; else
        GOBIN="$HOME/.local/bin"; mkdir -p "$GOBIN"; export PATH="$GOBIN:$PATH"
    fi
    say "Building tailscale + tailscaled via the Go module proxy (takes a few minutes)..."
    GOBIN="$GOBIN" go install tailscale.com/cmd/tailscale@latest tailscale.com/cmd/tailscaled@latest \
        || die "go install failed — see output above."
    say "Installed: $(tailscale version | head -1)"
fi

# ---- 2. Egress preflight ----------------------------------------------------
# The default (Trusted) network policy 403s *.tailscale.com at the egress
# proxy, and nothing below can work until it is allowed. Fail loud and early.
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 https://controlplane.tailscale.com/ 2>/dev/null || true)
if [[ "$code" == "000" || -z "$code" ]]; then
    cat >&2 <<'EOF'
ERROR: controlplane.tailscale.com is unreachable — the environment's network
policy is blocking Tailscale. Fix (owner, in the browser):

  claude.ai/code -> environment selector -> edit this environment
  -> Network access: Custom
  -> Allowed domains, add:        *.tailscale.com
     (optionally also:            log.tailscale.io)
  -> CHECK "Also include default list of common package managers"
  -> save, then start a session in the updated environment and re-run this.
EOF
    exit 2
fi

# ---- 3. Daemon --------------------------------------------------------------
# Userspace networking: needs no NET_ADMIN/TUN privileges, works in any
# container. Tailnet TCP dialing happens through the SOCKS5 listener —
# that is what examples/tws_tunnel.py plugs into.
if [[ ! -S "$SOCKET" ]] || ! ts status >/dev/null 2>&1; then
    mkdir -p "$STATE_DIR"
    say "Starting tailscaled (userspace networking, SOCKS5 on 127.0.0.1:$SOCKS_PORT)..."
    nohup tailscaled \
        --tun=userspace-networking \
        --statedir="$STATE_DIR" \
        --socket="$SOCKET" \
        --socks5-server="127.0.0.1:$SOCKS_PORT" \
        --outbound-http-proxy-listen="127.0.0.1:$HTTP_PROXY_PORT" \
        >>"$LOG" 2>&1 &
    for _ in $(seq 1 30); do [[ -S "$SOCKET" ]] && ts status >/dev/null 2>&1 && break; sleep 0.5; done
    [[ -S "$SOCKET" ]] || die "tailscaled did not start; log tail: $(tail -3 "$LOG" 2>/dev/null)"
fi

# ---- 4. Join the tailnet ----------------------------------------------------
state=$(backend_state)
if [[ "$state" == "Running" ]]; then
    say "Already connected."
elif [[ -n "${TS_AUTHKEY:-}" ]]; then
    say "Joining tailnet with TS_AUTHKEY as '$NODE_NAME'..."
    ts up --auth-key="$TS_AUTHKEY" --hostname="$NODE_NAME" --accept-dns=false --timeout=120s \
        || die "tailscale up failed; log tail: $(tail -5 "$LOG" 2>/dev/null)"
else
    # No key: `tailscale up` prints a login URL and blocks until the owner
    # approves it in a browser. Run it in the background and surface the URL.
    say "No TS_AUTHKEY set — using interactive browser login."
    up_out=$(mktemp)
    ts up --hostname="$NODE_NAME" --accept-dns=false --timeout=240s >"$up_out" 2>&1 &
    up_pid=$!
    url=""
    for _ in $(seq 1 240); do
        if [[ -z "$url" ]]; then
            url=$(grep -oE 'https://login\.tailscale\.com/a/[A-Za-z0-9]+' "$up_out" | head -1 || true)
            [[ -n "$url" ]] && { say ""; say ">>> AUTHENTICATE THIS NODE — open in a browser: $url"; say ""; }
        fi
        [[ "$(backend_state)" == "Running" ]] && break
        kill -0 "$up_pid" 2>/dev/null || break
        sleep 1
    done
    wait "$up_pid" 2>/dev/null || true
    [[ "$(backend_state)" == "Running" ]] || die "Not connected. tailscale up said: $(cat "$up_out")"
fi

# ---- 5. Report --------------------------------------------------------------
say ""
say "Connected. This node: $(ts ip -4 2>/dev/null | head -1) ($NODE_NAME)"
ts status || true
cat <<EOF

Next: forward the TWS port over the tailnet so repo scripts work unchanged —
  python examples/tws_tunnel.py <windows-machine-name> &
  python examples/tws_status.py
(The Windows side must be sharing TWS per docs/tailscale.md first.)
EOF
