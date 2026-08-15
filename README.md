# Medik

[![tests](https://github.com/alcademe67/Medik/actions/workflows/tests.yml/badge.svg)](https://github.com/alcademe67/Medik/actions/workflows/tests.yml)

IBKR trading automation. **Read [`CLAUDE.md`](CLAUDE.md) first** — it holds the
standing risk rules, the execution policy, and the account facts everything
else assumes.

## Repository layout

| directory | what's in it |
|---|---|
| `ibkr/` | TWS connection, orders, historical data, scanner, bar cache |
| `strategy/` | Indicators, signal gate, pullback strategy, scoring, risk, journal |
| `backtest/` | No-lookahead backtester, the commission gate, low-frequency comparison |
| `examples/` | Runnable entry points |
| `service/` | Windows background service |
| `tests/` | pytest suite — `python -m pytest tests/ -q` |
| `.github/workflows/` | CI: pytest on Linux + Windows, py3.11/3.12, plus a byte-compile pass |
| [`docs/`](docs/) | Operating docs: session handoff, service setup, core-holding runbook, backtest verdict, MCP servers |
| [`data/`](data/) | Cached daily bars. **Gitignored**; fill with `examples/fetch_bar_cache.py` |
| [`reports/`](reports/) | Generated report output. **Gitignored** — contains live balances |
| [`logs/`](logs/) | Service logs and `alerts.log`. **Gitignored** |
| [`notebooks/`](notebooks/) | Research notebooks (Jupyter deps are separate from `requirements.txt`) |

`paths.py` resolves the last four. `MEDIK_DATA_DIR`, `MEDIK_REPORTS_DIR` and
`SERVICE_LOG_DIR` move them off the repo drive; `notebooks/` is tracked in
git, so it stays put.

## Interactive Brokers TWS connection

Python connects to a locally running TWS (Trader Workstation) instance over its
socket API, using the [`ib_async`](https://github.com/ib-api-reloaded/ib_async)
library.

### 1. Configure TWS

In TWS, go to **File > Global Configuration > API > Settings** and set:

- **Enable ActiveX and Socket Clients** — checked
- **Socket port** — `7496` (TWS Live Trading; `7497` is Paper Trading)
- **Trusted IPs** — add `127.0.0.1`
- **Read-Only API** — unchecked, if you want to place orders from Python; checked
  if you only want read access

Leave TWS open and logged into the account you want to connect to — the API
only works while TWS itself is running.

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure connection settings

```bash
cp .env.example .env
```

Defaults are `127.0.0.1:7496` (TWS Live). Edit `.env` if your Global
Configuration uses a different port or you're running multiple TWS/Gateway
instances (each needs a distinct `IBKR_CLIENT_ID`).

### 4. Test the connection

```bash
python examples/connect_test.py
```

This prints account summary and current positions — read-only, no orders.

### 5. Placing orders

`ibkr/orders.py` has `place_limit_order` / `place_market_order` helpers. Both
require an explicit `confirm=True` — without it they raise `OrderRejected`
instead of submitting anything, so a script can't send a live order by
accident. See `examples/place_order_example.py`.

This is a **live trading account** — orders placed this way use real money.
Prefer limit orders over market orders, and double check symbol/side/quantity/
price before setting `confirm=True`.

## Installing the trader-dev MCP server

`scripts/Install-TraderDevMcp.ps1` registers the `trader-dev` SSE server with
Claude Code at **user scope**, which makes it available in all of your
projects. **PowerShell is the supported way to install this.** A bash script
is also included for Unix shells, but it is optional - see
[Optional: the bash installer](#optional-the-bash-installer).

## Windows, step by step

### Prerequisites

* **PowerShell.** Developed and tested against PowerShell 7.6 (`pwsh`). It
  avoids 7-only syntax and should work on the Windows-bundled 5.1, but that
  has not been verified - if you hit errors on 5.1, install 7 with
  `winget install Microsoft.PowerShell` and run `pwsh`. Check your version
  with `$PSVersionTable.PSVersion`.
* **Claude Code on your `PATH`.** `Get-Command claude` should resolve. The
  script stops with a clear message if it does not.
* **Git**, to clone this repository.

### 1. Get the files

```powershell
cd C:\Users\<you>\code          # wherever you keep repositories
git clone https://github.com/alcademe67/Medik.git
cd Medik
```

If you already have a clone, `git pull` instead.

### 2. Allow the script to run

Windows blocks unsigned scripts by default. Allow them for this session only,
which is undone as soon as you close the window:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

If you downloaded the repository as a ZIP rather than cloning it, Windows also
marks the files as coming from the internet. Clear that with
`Unblock-File .\scripts\Install-TraderDevMcp.ps1`.

### 3. Store your token (skip if the server needs no authentication)

Type the token at a prompt rather than putting it in a command, so it does not
land in your PowerShell history:

```powershell
$ss = Read-Host -Prompt 'trader-dev token' -AsSecureString
$plain = [System.Net.NetworkCredential]::new('', $ss).Password
Set-Content -Path ~\.trader-dev-token -Value $plain -NoNewline
Remove-Variable plain, ss
```

`Read-Host -AsSecureString` needs a real console window; it cannot be piped
from another script. The script also accepts `$env:TRADER_DEV_TOKEN`, but it
will refuse a token passed as a parameter, because that would leak into your
history and into the process list.

### 4. Install

```powershell
.\scripts\Install-TraderDevMcp.ps1 -TokenFile ~\.trader-dev-token
```

Without a token, run `.\scripts\Install-TraderDevMcp.ps1` on its own.
Re-running is safe: an entry that already matches is reported and left alone.
Use `-Force` to remove and re-add it.

### 5. Confirm it worked

Restart Claude Code, then run `/mcp`. The server should appear in the list.

### If it fails

Run the built-in probe before assuming the token is at fault:

```powershell
.\scripts\Install-TraderDevMcp.ps1 -Diagnose -TokenFile ~\.trader-dev-token
```

This matters because Claude Code blames the `Authorization` header for *any*
403 on the connection, so a VPN, corporate proxy, or CDN refusing the request
is reported as `Server rejected the configured Authorization header`. The probe
requests the endpoint with and without the header and tells you which side is
actually refusing. Pass `-TokenFile` here too - without it, only the
unauthenticated half of the check runs. It changes no configuration.

If the endpoint turns out to want a different scheme, or OAuth rather than a
header:

```powershell
# API-key style header instead of a bearer token
.\scripts\Install-TraderDevMcp.ps1 -HeaderName X-API-Key -AuthScheme '' -TokenFile ~\.trader-dev-token

# no header at all, then authorize through /mcp
.\scripts\Install-TraderDevMcp.ps1 -Force
```

Setting an `Authorization` header disables Claude Code's OAuth fallback, so if
the server expects a browser sign-in, install without a token.

### Removing it

```powershell
.\scripts\Install-TraderDevMcp.ps1 -Uninstall
Remove-Item ~\.trader-dev-token
```

Deleting the entry does not revoke the token at whoever issued it, and Claude
Code snapshots its config on every change - so a token installed earlier can
still be sitting in `~\.claude\backups\`. Check with:

```powershell
Select-String -Path ~\.claude\backups\* -Pattern '<your token prefix>'
```

## Optional: the bash installer

`scripts/install-trader-dev-mcp.sh` is an alternative for Linux, macOS, and
Git Bash. It is **not** required on Windows - prefer the PowerShell script
above, which avoids two Git Bash problems: `claude.cmd` is not always
resolvable from bash, and Git for Windows rewrites the script to CRLF on
checkout unless the `.gitattributes` in this repository is honoured, after
which bash fails with `$'\r': command not found`.

Behaviour matches the PowerShell version flag for flag:

```bash
./scripts/install-trader-dev-mcp.sh                              # install, no auth
./scripts/install-trader-dev-mcp.sh --token-file ~/.trader-dev-token
./scripts/install-trader-dev-mcp.sh --diagnose --token-file ~/.trader-dev-token
./scripts/install-trader-dev-mcp.sh --force
./scripts/install-trader-dev-mcp.sh --uninstall
./scripts/install-trader-dev-mcp.sh --help
```

## A note on the endpoint

`https://mcp.trader.dev/sse` was unreachable from the environment these
scripts were written in, so its authentication scheme is unverified.
`Authorization: Bearer` is the default because it is the common case, not
because it was confirmed. If `-Diagnose` reports that the server rejects the
header, try `X-API-Key` or install without a token and use OAuth.
