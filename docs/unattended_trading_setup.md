# Unattended MEDIK ETF trading — IBC + paid API data setup

Goal: let the bot run and trade **while you're away**, with no daily clicking and
no one typing a password into a prompt. This removes the two things that today
require a human at the keyboard.

> **Claude never does the credential steps in here.** Every place your username,
> password, or 2FA is entered, *you* enter it — into IBKR's own app or IBC's own
> config file, on your machine. Claude will not fill those in, will not read them
> back, and does not need them to do anything else in this repo.

> **Read this first (unchanged by any of the below):** the ETF strategy has a
> *negative* tested expectancy (v2 @ $500 ≈ −25.9%, no edge before costs — see
> CLAUDE.md). Making it run unattended does not make it profitable; it makes it
> lose money without you watching. The setup below is "how", not "should".

---

## Why two pieces are needed

The bot needs two separate IBKR connections, and each has its own login problem:

| Path | What it's for | Today's problem | Fix |
|---|---|---|---|
| **TWS socket** (127.0.0.1:7496) | places orders, pulls history | daily/weekly manual login | **IBC** auto-login |
| **Client Portal Gateway** (localhost:5000) | real-time quotes | browser SSO expires ~daily; can't be automated safely | **Paid API data** — deletes this path entirely |

The Client Portal Gateway only exists as a workaround: the *free* real-time feed
returns **error 10089** on the TWS API ("requires additional subscription FOR
API") — it's licensed for IBKR's own apps only. A **paid, API-entitled**
subscription makes the TWS socket serve real-time directly, so the gateway (and
its un-automatable browser login) is no longer needed. That is what makes true
unattended operation possible.

---

## PART A — Paid API-entitled market data (removes the gateway dependency)

1. Confirm equity is above IBKR's market-data minimum (~USD 500). The pending
   $200 deposit covers this; below it, market data is disabled regardless of
   subscription.
2. **Before paying, ask IBKR directly** (Client Portal → Help → secure message,
   or phone): *"Which market-data subscription gives real-time **API** (TWS
   socket) access for US equities/ETFs on ARCA, NASDAQ and BATS for account
   U26953060?"* This matters because the **free** "US Real-Time Non-Consolidated
   Streaming Quotes" you already have is **not** API-entitled — that is the exact
   cause of error 10089. Get the API-entitlement confirmed in writing before you
   pay, given that history.
3. Subscribe to the one IBKR confirms (Client Portal → **Settings → User
   Settings → Market Data Subscriptions**). It typically activates the next
   trading day, and is billed to the account monthly.
4. **Verify empirically — do not switch on faith.** After it activates, Claude
   runs a socket quote test: real-time counts only if `marketDataType == 1` with
   a live bid/ask and no 10089. On-screen prices in TWS do **not** prove the API
   is entitled (that mistake is documented in CLAUDE.md).
5. Only once verified, flip the bot's quote source from the gateway to the
   socket: in `run_medik_etf.bat`, change
   `set MEDIK_ETF_QUOTE_SOURCE=cpapi` → `set MEDIK_ETF_QUOTE_SOURCE=tws`.
   (Keep the `cpapi` backup line noted; if the socket test ever fails, switch
   back.) After this, the Client Portal Gateway and its login are no longer used.

---

## PART B — IBC for unattended TWS login

IBC (IBController) auto-starts and auto-logs-in TWS or IB Gateway, so a restart
doesn't leave you logged out. It's the standard tool for unattended IBKR bots.

1. Download from **https://github.com/IbcAlpha/IBC** (Releases → the Windows
   `.zip` matching your TWS major version). Unzip to e.g. `C:\IBC`.
2. Decide TWS vs IB Gateway:
   - **Keep TWS** (port 7496) → no code change; the repo already targets 7496.
   - IB Gateway (port 4001) is lighter/headless but would need the bot's port
     changed. **Recommended: stay on TWS/7496** to avoid touching code.
3. Edit `C:\IBC\config.ini` (**this is where you — not Claude — put credentials**):
   - `IbLoginId=alcademe67`
   - `IbPassword=` *(your TWS password — you type it here, on your machine)*
   - `TradingMode=live`
   - `IbDir=` your TWS install path
   - `AcceptNonBrokerageAccountWarning=yes`
   - Leave the API/read-only settings alone; the API is configured in TWS itself
     (step 5).
4. **Two-factor authentication — the honest catch.** IBKR requires 2FA, and it
   is the one thing IBC cannot fully suppress:
   - With **IBKR Mobile** as your 2FA method, the daily auto-restart sends a
     push you approve with one tap on your phone — so "fully away" still needs
     that tap once a day. Set IBKR's auto-restart time (see below) to a time
     you're reliably near your phone.
   - IBC's `ReloginAfterSecondFactorAuthenticationTimeout=yes` and
     `SecondFactorAuthenticationExemptionHours` reduce how often the tap is
     needed within a window — read IBC's User Guide and set these to taste.
   - There is **no honest way to make 2FA require zero human action** without
     weakening account security, so don't expect one. Plan for a daily phone
     tap, not a keyboard login.
5. In TWS: **File → Global Configuration → API → Settings** — enable *ActiveX
   and Socket Clients*, socket port **7496**, add **127.0.0.1** to Trusted IPs,
   and leave *Read-Only API* **off** (the bot must place orders).
6. In TWS: **Lock and Exit → Auto restart** — set the daily restart time
   (IBKR forces a daily restart; align it with when you can do the 2FA tap).
   With IBC managing startup, TWS comes back logged in after that restart.
7. Point Windows at IBC instead of raw TWS at logon: run IBC's
   `StartTWS.bat` (edit its `TWS_MAJOR_VRSN` and paths first). You can drop a
   shortcut to it in the Startup folder the same way the supervisor's launcher
   is set up.

---

## PART C — what stays, what changes in this repo

- **Keep** the connection supervisor (`ops/medik_supervisor.py`) — with paid
  data it still watches the socket and the bot process; if you *don't* move to
  paid data, it's what keeps the gateway healed and auto-reauthenticated.
- **Change** only `MEDIK_ETF_QUOTE_SOURCE` (Part A step 5), and only after the
  empirical socket test passes.
- The bot's arming (`MEDIK_ETF_LIVE=true`, the enabled scheduled task) and all
  18 authorize_order gates and risk controls are unchanged.

---

## PART D — verification checklist before trusting it unattended

Claude can run every check here except the ones marked *(you)*:

1. *(you)* IBC brings TWS up logged-in after a manual restart, no keyboard login.
2. *(you)* A simulated daily auto-restart leaves TWS logged in (one 2FA tap).
3. Socket real-time confirmed: `marketDataType == 1`, live bid/ask, no 10089.
4. `MEDIK_ETF_QUOTE_SOURCE=tws` and the bot's preflight logs `QUOTE SESSION: OK`.
5. Bot preflight passes end-to-end in dry-run against the socket feed.
6. Equity ≥ $500 so market data stays enabled.
7. Supervisor running; scheduled task enabled; STOP_MEDIK kill-switch understood.

Only when 1–7 all pass is it genuinely "runs while you're away" — and even then,
expect one 2FA tap a day, and remember the tested expectancy is negative.

---

## The one line that never changes

Claude will not store, type, or read your password or 2FA codes — not for TWS,
not for the gateway, not in IBC's config. Those steps are yours. Everything
else — the health-watching, reauthenticating, verifying, and (once you've armed
it) the trading itself — runs without you.
