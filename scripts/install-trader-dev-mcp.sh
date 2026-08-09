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
#   # probe the endpoint and explain what a failure actually means;
#   # changes no configuration
#   ./install-trader-dev-mcp.sh --diagnose --token-file ~/.trader-dev-token
#
# --diagnose exits 0 when the endpoint is usable as configured, 1 otherwise.
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
DIAGNOSE=0
TIMEOUT=20

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
        --diagnose)    DIAGNOSE=1; shift ;;
        --timeout)     TIMEOUT="${2:-}"; [ -n "$TIMEOUT" ] || die "--timeout needs a value"; shift 2 ;;
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

if [ "$DIAGNOSE" -eq 1 ]; then
    command -v curl >/dev/null 2>&1 || die "--diagnose needs curl on PATH"

    is2xx() { case "$1" in 2??) return 0 ;; *) return 1 ;; esac; }

    # Emits "<http_code> <curl_exit>". The code is 000 when no HTTP response
    # arrived at all - DNS failure, refused CONNECT, TLS error. A healthy SSE
    # endpoint answers 200 and then holds the stream open, so curl exit 28
    # (timeout) alongside a 2xx is a success, not a fault.
    probe() {
        local code rc
        # Both types, matching what an MCP client negotiates. Sending only
        # text/event-stream makes strict servers answer 415, which would look
        # like a fault in the endpoint rather than an artifact of the probe.
        code="$(curl -sS -o /dev/null -w '%{http_code}' \
                     --max-time "$TIMEOUT" \
                     -H 'Accept: application/json, text/event-stream' \
                     "$@" "$URL" 2>/dev/null)" && rc=0 || rc=$?
        [ -n "$code" ] || code="000"
        printf '%s %s' "$code" "$rc"
    }

    # A CDN or WAF sitting in front of the origin blocks with the same status
    # codes the origin would use, so the response headers are the only way to
    # tell "the edge refused you" from "the MCP server refused you".
    # $1 is the status observed. A CDN reporting 404 or 200 is just relaying the
    # origin, so the interesting case is a block-shaped status: those the edge
    # can generate entirely on its own, without the origin ever being consulted.
    report_edge_marker() {
        local observed="$1" hdrs srv ray
        hdrs="$(curl -sS -o /dev/null -D - --max-time "$TIMEOUT" \
                     -H 'Accept: application/json, text/event-stream' \
                     "$URL" 2>/dev/null | tr -d '\r')" || return 0
        srv="$(printf '%s\n' "$hdrs" | awk -F': ' 'tolower($1) == "server" { print $2; exit }')"
        ray="$(printf '%s\n' "$hdrs" | awk -F': ' 'tolower($1) == "cf-ray" { print $2; exit }')"
        [ -n "$srv$ray" ] || return 0
        note ""
        note "Served via an edge/CDN${srv:+ (server: $srv)}${ray:+, cf-ray: $ray}."
        case "$observed" in
            403|429|503)
                note "A $observed is a status the edge itself can return, so this may be CDN"
                note "bot protection refusing a non-browser client, with the MCP server"
                note "behind it never seeing the request. Retrying with a browser User-Agent"
                note "distinguishes the two: if it succeeds, the edge was the blocker."
                ;;
            *)
                note "A $observed is normally relayed from the origin rather than produced by"
                note "the edge, so the CDN is probably not the cause here."
                ;;
        esac
    }

    note "Probing $URL (timeout ${TIMEOUT}s)"
    for proxy_var in HTTPS_PROXY https_proxy ALL_PROXY all_proxy; do
        if [ -n "${!proxy_var:-}" ]; then
            note "  warning: $proxy_var is set, so a proxy sits between you and the"
            note "           endpoint. A proxy's own refusal is indistinguishable"
            note "           from the server's here - weigh the verdict accordingly."
            break
        fi
    done

    read -r bare_code bare_rc <<<"$(probe)"
    note "  without auth header:      HTTP $bare_code"

    auth_code=""
    auth_rc=0
    if [ -n "$TOKEN" ]; then
        if [ -n "$AUTH_SCHEME" ]; then
            probe_header="$HEADER_NAME: $AUTH_SCHEME $TOKEN"
        else
            probe_header="$HEADER_NAME: $TOKEN"
        fi
        read -r auth_code auth_rc <<<"$(probe -H "$probe_header")"
        note "  with $HEADER_NAME header: HTTP $auth_code"
    else
        note "  with auth header:         skipped (no token supplied)"
    fi

    note ""
    if [ "$bare_code" = "000" ] && { [ -z "$TOKEN" ] || [ "$auth_code" = "000" ]; }; then
        note "VERDICT: no HTTP response at all (curl exit $bare_rc)."
        note "This is not an authentication problem - the connection never completed,"
        note "so no credential was ever sent. Check DNS, TLS interception, VPN, or a"
        note "proxy denying CONNECT."
        exit 1
    fi

    if [ -z "$TOKEN" ]; then
        if is2xx "$bare_code"; then
            note "VERDICT: endpoint is reachable and serves requests without auth."
            exit 0
        elif [ "$bare_code" = "401" ]; then
            note "VERDICT: endpoint is reachable and requires authentication."
            note "Supply a token with --token-file, or install without one and let"
            note "OAuth run via /mcp."
            exit 1
        fi
        note "VERDICT: endpoint returned $bare_code to an unauthenticated request."
        note "Re-run with --token-file to test an authenticated one."
        exit 1
    fi

    if is2xx "$auth_code"; then
        note "VERDICT: the endpoint accepted the $HEADER_NAME header (HTTP $auth_code)."
        [ "$auth_rc" -eq 28 ] && note "curl timed out holding the stream open, which is normal for SSE."
        note "If Claude Code still reports a failure, the problem is its transport"
        note "setting rather than the credential - try --transport http."
        exit 0
    elif [ "$auth_code" = "401" ]; then
        # Checked before the identical-response test below: 401 both with and
        # without the header still means the credential failed to satisfy the
        # server, which is the opposite of what "identical" would imply.
        note "VERDICT: the server returned 401 to the authenticated request."
        note "The credential was sent and did not satisfy it - the token is wrong,"
        note "expired, revoked, or carries the wrong scheme."
        note "Worth trying:"
        note "  $0 --header-name X-API-Key --auth-scheme '' --token-file <file>"
        note "  $0 --force        # no header, then authorize via /mcp if it uses OAuth"
        exit 1
    elif [ "$bare_code" = "$auth_code" ]; then
        note "VERDICT: identical response ($auth_code) with and without the header."
        note "The rejection is therefore NOT about your token - the request is"
        note "refused either way. Suspect a proxy or VPN in the path, an IP-level"
        note "block, or simply the wrong URL."
        report_edge_marker "$auth_code"
        exit 1
    elif is2xx "$bare_code"; then
        note "VERDICT: works WITHOUT the header ($bare_code), fails WITH it ($auth_code)."
        note "The header is breaking an endpoint that does not want one."
        note "Reinstall without a token:  $0 --force"
        exit 1
    fi

    note "VERDICT: the server sees the header and rejects it (HTTP $auth_code,"
    note "versus $bare_code unauthenticated). The credential or the scheme is wrong."
    note "Worth trying:"
    note "  $0 --header-name X-API-Key --auth-scheme '' --token-file <file>"
    note "  $0 --force        # no header, then authorize via /mcp if it uses OAuth"
    exit 1
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
