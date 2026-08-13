# Additional MCP servers

The IBKR connector is already attached to this account and is where market
data and account state come from. This file covers registering *extra* MCP
servers with Claude Code.

`scripts/Install-TraderDevMcp.ps1` is named for its default target but is
fully parameterized (`-Name`, `-Url`, `-Transport`, `-HeaderName`,
`-AuthScheme`, `-TokenFile`), so it installs any bearer-token MCP server.
Its real value is `-Diagnose`: Claude Code reports *any* 403 on a connection
as "Server rejected the configured Authorization header", so a VPN, proxy, or
CDN refusing the request looks identical to a bad token. The probe requests
the endpoint with and without the header and tells you which side is
actually refusing.

---

## Quiver Quantitative

Congressional trading, government contracts, lobbying spend, insider (Form 4)
transactions, r/WallStreetBets activity, dark-pool short volume, and 13F
changes.

| | |
|---|---|
| Endpoint | `https://mcp.quiverquant.com/` |
| Auth | `Authorization: Bearer <API key>` |
| Key from | <https://www.quiverquant.com/> |
| Cost | **paid — API access starts around $30/month** |
| In the claude.ai connector directory? | **No.** Install it in Claude Code. |

> Verified by web search, not by reading the vendor docs directly:
> `api.quiverquant.com` and `pulsemcp.com` are both blocked by this
> environment's egress proxy. Treat the transport in particular as
> unconfirmed — if `http` fails, retry with `sse`.

### Install (PowerShell, on the Windows machine)

Store the key in a file rather than typing it into a command, so it does not
land in PowerShell history:

```powershell
$ss = Read-Host -Prompt 'Quiver API key' -AsSecureString
$plain = [System.Net.NetworkCredential]::new('', $ss).Password
Set-Content -Path ~\.quiver-token -Value $plain -NoNewline
Remove-Variable plain, ss
```

Probe first, then install:

```powershell
.\scripts\Install-TraderDevMcp.ps1 -Name quiver -Url https://mcp.quiverquant.com/ `
    -Transport http -TokenFile ~\.quiver-token -Diagnose

.\scripts\Install-TraderDevMcp.ps1 -Name quiver -Url https://mcp.quiverquant.com/ `
    -Transport http -TokenFile ~\.quiver-token
```

Restart Claude Code and run `/mcp` — `quiver` should be listed.

### Install (plain CLI, any platform)

```bash
claude mcp add --transport http --scope user \
  quiver https://mcp.quiverquant.com/ \
  --header "Authorization: Bearer $(cat ~/.quiver-token)"
```

Swap `--transport http` for `--transport sse` if the connection fails.

### Removing it

```powershell
.\scripts\Install-TraderDevMcp.ps1 -Name quiver -Uninstall
Remove-Item ~\.quiver-token
```

Deleting the entry does not revoke the key at Quiver, and Claude Code
snapshots its config on every change — a key installed earlier can still be
sitting in `~\.claude\backups\`:

```powershell
Select-String -Path ~\.claude\backups\* -Pattern '<your key prefix>'
```

### Before subscribing — two things worth weighing

**The cost is large relative to this account.** ~$30/month is ~$360/year
against a net liquidation of roughly $292. That is a >100%/yr drag on the
account, on top of a commission structure that already costs ~2% per round
trip and that `docs/backtest-verdict.md` shows was enough on its own to turn
a +1.57% two-year gross result into −35.9% net. Data that has to pay for
itself out of *this* account cannot. Worth it as a research subscription
funded separately; not as a trading expense here.

**Anything it surfaces is a hypothesis, not a signal.** The standing rule is
unchanged: a strategy is not "working" until it has been re-run through
`backtest/net_of_commission.py`. Congressional-trade following in particular
is a well-known, widely-front-run, low-frequency signal — plausible as
research input, and exactly the kind of thing that looks compelling in a
screenshot and fails a costed backtest. And the adopted strategy is
deliberately inert; new data is not a reason to start trading it.

**Never wire an MCP server into an execution path.** Execution policy is
unchanged: Claude drafts, the owner submits.

---

## trader-dev

The server the installer defaults to. See the main [`README.md`](../README.md)
for its full walkthrough. Its endpoint (`https://mcp.trader.dev/sse`) was
unreachable from the environment the scripts were written in, so its
authentication scheme is **unverified** — `Authorization: Bearer` is the
default because it is the common case, not because it was confirmed.
