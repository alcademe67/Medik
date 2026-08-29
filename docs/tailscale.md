# Tailscale: reaching TWS from Claude cloud sessions

## Why

Every script in `ibkr/`/`examples/` dials TWS at `127.0.0.1:7496` — which only
exists on the owner's Windows machine. Claude's remote (cloud) sessions run in
an ephemeral container that cannot see that machine, so live work (a
`tws_status.py` audit, a `fetch_bar_cache.py` refresh, sizing off live settled
cash) has always required the owner at the desk. Joining the container and the
Windows machine to the same Tailscale tailnet closes that gap: the container
reaches TWS over an encrypted peer-to-peer overlay with nothing exposed to the
public internet.

**This changes reachability, not policy.** The execution policy in CLAUDE.md is
unchanged: Claude never places live orders autonomously; order scripts still
require a human typing YES; unattended automation stays paper-only.

## How it works (container side)

`scripts/tailscale_up.sh` builds the official client from source through the
Go module proxy (`pkgs.tailscale.com` is not on the environment allowlist;
`proxy.golang.org` is), then runs `tailscaled` in **userspace-networking**
mode — no TUN device or elevated network privileges needed. In that mode
tailnet TCP dials happen through a local SOCKS5 listener (`127.0.0.1:1055`),
which `ib_async` can't speak — so `examples/tws_tunnel.py` bridges it:

    container 127.0.0.1:7496  →  SOCKS5 1055  →  tailnet  →  windows:7496

`IBKRClient` defaults to `127.0.0.1:7496` (`ibkr/client.py`), so with the
tunnel up, **every existing script works unchanged**. Traffic rides Tailscale's
DERP relays over HTTPS via the session's egress proxy (the container has no
UDP egress); fine for API calls, not a low-latency path.

## One-time setup

### 1. Claude environment — allow the Tailscale domains (the current blocker)

At **claude.ai/code → environment selector → edit the environment**:

- **Network access: Custom**
- Allowed domains, one per line:
  - `*.tailscale.com`  (control plane, login, DERP relays)
  - `log.tailscale.io`  (optional; quiets log-upload errors)
- **Check "Also include default list of common package managers"** — otherwise
  pip/go/npm break, including the Go module proxy this setup builds from.

Verified 2026-08-29: without this, the egress proxy answers
`CONNECT controlplane.tailscale.com:443 → 403 Forbidden` and nothing joins.
Policy edits are per-environment and take effect for sessions started in the
updated environment.

### 2. Claude environment — auth key (recommended)

In the same dialog, add an environment variable so sessions join without a
browser round-trip:

- Generate at <https://login.tailscale.com/admin/settings/keys> →
  **Reusable + Ephemeral**, ideally with a tag (e.g. `tag:medik-cloud`).
  Ephemeral means the node evaporates when the container dies — exactly right
  here, since every session is a fresh container. Keys expire (90 days max);
  regenerate when joins start failing with an auth error.
- Set it as `TS_AUTHKEY`.

Without `TS_AUTHKEY`, `tailscale_up.sh` prints a `https://login.tailscale.com/a/…`
URL to approve in a browser — fine for a one-off, annoying every session.

### 3. Windows machine — join and share TWS

1. Install Tailscale (<https://tailscale.com/download>), sign in to the same
   tailnet. Note the machine name (`tailscale status`, or the admin console
   Machines page) — that's the name passed to `tws_tunnel.py`.
2. Share the TWS socket with the tailnet **via `tailscale serve`** (admin
   PowerShell on the Windows machine):

       tailscale serve --bg --tcp=7496 tcp://127.0.0.1:7496

   Verify with `tailscale serve status`; remove with
   `tailscale serve --tcp=7496 off`. (Syntax moves between versions — if
   rejected, see `tailscale serve --help`.)

   Why serve and not TWS trusted IPs: serve terminates on the Windows
   tailscaled and re-dials `127.0.0.1:7496`, so **TWS sees the connection from
   127.0.0.1, which is already trusted** (the whole chain was verified working
   on 2026-08-07 via `check_tws.bat`). No TWS config change, no Windows
   firewall rule, and — decisive — no per-session churn: each ephemeral
   container gets a *new* 100.x IP, so a TWS trusted-IP entry would need
   editing every single session.

   **Never use `tailscale funnel` here** — funnel publishes to the public
   internet. `serve` is tailnet-only.

### 4. Tailnet ACL — contain the blast radius (recommended)

`serve` extends TWS's localhost trust to the tailnet, and a fresh tailnet's
ACL is allow-all. In the admin console (Access Controls), restrict the cloud
node's tag to the one port it needs, e.g.:

    "acls": [
      {"action": "accept", "src": ["autogroup:member"], "dst": ["*:*"]},
      {"action": "accept", "src": ["tag:medik-cloud"], "dst": ["windows-machine-name:7496"]}
    ]

(with `"tagOwners": {"tag:medik-cloud": ["autogroup:admin"]}`, and the first
rule narrowed to taste). Optional extra brake: TWS Global Config → API →
Settings → **Read-Only API** blocks order placement at the socket for *all*
API clients — including `place_core_holding.py` run locally — so it's a
tradeoff, not a default.

## Per-session use (after setup)

    bash scripts/tailscale_up.sh                     # build if needed, join
    python examples/tws_tunnel.py <windows-name> &   # 127.0.0.1:7496 -> tailnet
    python examples/tws_status.py                    # any script, unchanged

`tws_tunnel.py --paper` forwards 7497 instead for the paper account.
`scripts/tailscale_up.sh status` shows daemon/tailnet state. TWS must be open
and logged in on the Windows machine, as always.

## Status (2026-08-29)

Everything container-side is proven: the client (v1.102.3) builds via the Go
module proxy in a few minutes, `tailscaled` runs in userspace mode and
correctly routes through the session's egress proxy, and the tunnel's
forwarding logic passes a local SOCKS5 round-trip test. The join itself stops
at the network policy (step 1) — until an environment allows
`*.tailscale.com`, `tailscale up` fails with `403 Forbidden` from the egress
proxy. Steps 2–4 have not been exercised yet; expect first-run friction in
`tailscale serve` syntax and the ACL.

## Troubleshooting

- **`connection refused` from the tunnel's reachability check** — the tailnet
  path works but nothing answers: TWS not open, or `tailscale serve` not
  configured on the Windows side.
- **`host unreachable`** — wrong machine name, or the Windows machine is
  offline / logged out of Tailscale.
- **Join hangs with no URL and the log shows `403 Forbidden`** — network
  policy (step 1) not applied to this session's environment.
- **Auth key rejected** — key expired (90-day max) or single-use; regenerate
  as Reusable + Ephemeral.
- Tailscale login is **unrelated to the one-IBKR-login-per-username rule** —
  it kicks nothing off; the owner's TWS/phone sessions are untouched.
