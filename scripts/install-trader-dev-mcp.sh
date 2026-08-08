#!/usr/bin/env bash
#
# Install the trader-dev MCP server into Claude Code.
#
# Run this on the machine where you actually use Claude Code. User scope is
# the "global" one: the server becomes available in all of your projects and
# is stored in ~/.claude.json.
#
# Authentication is optional. Supply a token and the server is registered with
# an "Authorization: Bearer <token>" header; omit it and the server is
# registered unauthenticated. The token is read from the environment or from a
# file - never from a command-line argument, which would leak it into your
# shell history and into the process list.
#
# Usage:
#   ./install-trader-dev-mcp.sh                          # no auth
#   TRADER_DEV_TOKEN=pk_... ./install-trader-dev-mcp.sh  # bearer token
#   ./install-trader-dev-mcp.sh --token-file ~/.trader-dev-token
#   ./install-trader-dev-mcp.sh --force
#   ./install-trader-dev-mcp.sh --scope project
#   ./install-trader-dev-mcp.sh --name my-trader --url https://example/sse
#   ./install-trader-dev-mcp.sh --header-name X-API-Key --auth-scheme ''
#   ./install-trader-dev-mcp.sh --uninstall
#
set -euo pipefail

NAME="trader-dev"
URL="https://mcp.trader.dev/sse"
TRANSPORT="sse"
SCOPE="user"
HEADER_NAME="Authorization"
AUTH_SCHEME="Bearer"
TOKEN_FILE=""
FORCE=0
UNINSTALL=0

# Read from the environment so the secret never appears as an argv entry.
TOKEN="${TRADER_DEV_TOKEN:-}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }

usage() {
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --name)        NAME="${2:-}"; [ -n "$NAME" ] || die "--name needs a value"; shift 2 ;;
        --url)         URL="${2:-}"; [ -n "$URL" ] || die "--url needs a value"; shift 2 ;;
        --transport)   TRANSPORT="${2:-}"; [ -n "$TRANSPORT" ] || die "--transport needs a value"; shift 2 ;;
        --scope)       SCOPE="${2:-}"; [ -n "$SCOPE" ] || die "--scope needs a value"; shift 2 ;;
        --token-file)  TOKEN_FILE="${2:-}"; [ -n "$TOKEN_FILE" ] || die "--token-file needs a value"; shift 2 ;;
        --header-name) HEADER_NAME="${2:-}"; [ -n "$HEADER_NAME" ] || die "--header-name needs a value"; shift 2 ;;
        --auth-scheme) AUTH_SCHEME="${2-}"; shift 2 ;;  # may legitimately be empty
        --token)       die "refusing --token: pass the secret via the TRADER_DEV_TOKEN env var or --token-file instead" ;;
        --force)       FORCE=1; shift ;;
        --uninstall)   UNINSTALL=1; shift ;;
        -h|--help)     usage ;;
        *)             die "unknown argument: $1 (try --help)" ;;
    esac
done

case "$SCOPE" in
    user|project|local) ;;
    *) die "--scope must be one of: user, project, local (got '$SCOPE')" ;;
esac

if [ -n "$TOKEN_FILE" ]; then
    [ -f "$TOKEN_FILE" ] || die "token file not found: $TOKEN_FILE"
    # Strip a trailing newline; a stray one would be sent as part of the header.
    TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
    [ -n "$TOKEN" ] || die "token file is empty: $TOKEN_FILE"
fi

command -v claude >/dev/null 2>&1 \
    || die "the 'claude' CLI is not on PATH. Install Claude Code first: https://claude.com/claude-code"

# `claude mcp get` exits non-zero when the server is not configured.
existing_output=""
if existing_output="$(claude mcp get "$NAME" 2>/dev/null)"; then
    exists=1
else
    exists=0
fi

if [ "$UNINSTALL" -eq 1 ]; then
    if [ "$exists" -eq 0 ]; then
        note "Nothing to do: '$NAME' is not configured at any scope."
        exit 0
    fi
    claude mcp remove "$NAME" -s "$SCOPE"
    note "Removed '$NAME' from $SCOPE config."
    note "Any token stored with it is gone from the config, but is still valid"
    note "at the issuer until you revoke it there."
    exit 0
fi

if [ "$exists" -eq 1 ]; then
    if [ -n "$TOKEN" ]; then
        # The stored header is never read back, so a matching URL says nothing
        # about whether the stored credential matches this one. Always replace.
        note "Replacing existing '$NAME' entry (stored credentials cannot be verified)..."
    elif [ "$FORCE" -eq 0 ] && printf '%s' "$existing_output" | grep -qF -- "$URL"; then
        note "'$NAME' is already configured with $URL - nothing to do."
        note "Re-run with --force to remove and re-add it."
        exit 0
    else
        note "Replacing existing '$NAME' entry..."
    fi
    # Removal is best-effort: the entry may live at a different scope than the
    # one we are about to write to, in which case this is a no-op.
    claude mcp remove "$NAME" -s "$SCOPE" >/dev/null 2>&1 || true
fi

# The positional name/URL must precede the flags: `claude mcp add` declares
# --header as variadic, so it swallows any positional argument that follows it.
add_args=("$NAME" "$URL" --transport "$TRANSPORT" --scope "$SCOPE")
if [ -n "$TOKEN" ]; then
    if [ -n "$AUTH_SCHEME" ]; then
        add_args+=(--header "$HEADER_NAME: $AUTH_SCHEME $TOKEN")
    else
        add_args+=(--header "$HEADER_NAME: $TOKEN")
    fi
    note "Using auth header '$HEADER_NAME' (token supplied, ${#TOKEN} chars)."
else
    note "No token supplied - registering without an auth header."
    note "Set TRADER_DEV_TOKEN or pass --token-file to add one."
fi

claude mcp add "${add_args[@]}"

note ""
note "Configured entry:"
# `claude mcp get` prints configured headers, so redact anything that looks
# like a credential before it reaches the terminal or a CI log.
claude mcp get "$NAME" | sed -E 's/^([[:space:]]*[A-Za-z-]+:[[:space:]]*(Bearer[[:space:]]+)?)[A-Za-z0-9._~+\/-]{12,}=*$/\1<redacted>/' \
    || die "the server was added but could not be read back"

if [ -n "$TOKEN" ]; then
    note ""
    note "Note: Claude Code stores this header in plain text in its config file."
    note "Keep that file readable only by you:"
    note "  chmod 600 ~/.claude.json"
fi

note ""
note "Next steps:"
note "  1. Restart Claude Code so it picks up the new server."
note "  2. Run /mcp inside Claude Code to check its connection status."
note "     If it reports that authentication is needed, /mcp will walk you"
note "     through the login flow in your browser."
note ""
note "If the status looks wrong, confirm the host is reachable from this"
note "machine before assuming it is an auth problem:"
note "  curl -sS -i --max-time 20 '$URL'"
note ""
note "To undo: $0 --uninstall --scope $SCOPE"
