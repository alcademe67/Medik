#!/usr/bin/env bash
#
# Install the trader-dev MCP server into Claude Code.
#
# Run this on the machine where you actually use Claude Code. User scope is
# the "global" one: the server becomes available in all of your projects and
# is stored in ~/.claude.json.
#
# Usage:
#   ./install-trader-dev-mcp.sh              # install at user scope
#   ./install-trader-dev-mcp.sh --force      # replace any existing entry
#   ./install-trader-dev-mcp.sh --scope project
#   ./install-trader-dev-mcp.sh --name my-trader --url https://example/sse
#   ./install-trader-dev-mcp.sh --uninstall
#
set -euo pipefail

NAME="trader-dev"
URL="https://mcp.trader.dev/sse"
TRANSPORT="sse"
SCOPE="user"
FORCE=0
UNINSTALL=0

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }

usage() {
    sed -n '3,15p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --name)      NAME="${2:-}"; [ -n "$NAME" ] || die "--name needs a value"; shift 2 ;;
        --url)       URL="${2:-}"; [ -n "$URL" ] || die "--url needs a value"; shift 2 ;;
        --transport) TRANSPORT="${2:-}"; [ -n "$TRANSPORT" ] || die "--transport needs a value"; shift 2 ;;
        --scope)     SCOPE="${2:-}"; [ -n "$SCOPE" ] || die "--scope needs a value"; shift 2 ;;
        --force)     FORCE=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help)   usage ;;
        *)           die "unknown argument: $1 (try --help)" ;;
    esac
done

case "$SCOPE" in
    user|project|local) ;;
    *) die "--scope must be one of: user, project, local (got '$SCOPE')" ;;
esac

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
    exit 0
fi

if [ "$exists" -eq 1 ]; then
    if [ "$FORCE" -eq 0 ] && printf '%s' "$existing_output" | grep -qF -- "$URL"; then
        note "'$NAME' is already configured with $URL — nothing to do."
        note "Re-run with --force to remove and re-add it."
        exit 0
    fi
    note "Replacing existing '$NAME' entry..."
    # Removal is best-effort: the entry may live at a different scope than
    # the one we are about to write to, in which case this is a no-op.
    claude mcp remove "$NAME" -s "$SCOPE" >/dev/null 2>&1 || true
fi

claude mcp add --transport "$TRANSPORT" --scope "$SCOPE" "$NAME" "$URL"

note ""
note "Configured entry:"
claude mcp get "$NAME" || die "the server was added but could not be read back"

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
