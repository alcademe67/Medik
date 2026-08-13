# Running Claude Code locally (instead of in the cloud)

Written 2026-08-12, at the end of a cloud session, so a local session can
pick up exactly where that one left off.

## Why bother

Cloud sessions run in a datacenter container. TWS listens on **your PC's**
`127.0.0.1:7496`, which that container cannot reach — it has its own
loopback with nothing on it. Everything else works from the cloud (the IBKR
MCP connector reads the account fine), but the TWS socket does not, and no
setting changes that.

A local session runs on your machine, on the right side of that wall. It can:

- run `check_tws.bat` / `examples/tws_status.py` itself
- run `examples/place_core_holding.py` against your live TWS
- see hand-typed GUI orders via `reqAllOpenOrders`, which the MCP connector
  under-reports

What does **not** change:

- **The execution policy still applies.** `ibkr/orders.py` requires
  `confirm=True`; `place_core_holding.py` requires a typed `YES`. A local
  session can reach TWS, but nothing here submits an order without a human
  saying so. Don't remove those gates.
- **The commission arithmetic doesn't change.** A $1.00 minimum on a small
  position is the same drag wherever Claude is running. See the backtest
  verdict in CLAUDE.md.

## Setup

1. Install **Claude Code** — the Windows desktop app is easiest; the CLI
   works too (`npm install -g @anthropic-ai/claude-code`, needs Node).
2. Open it in your existing Medik folder — the one containing
   `check_tws.bat`.
3. `git pull` first. The cloud session pushed to
   `claude/interactive-broker-python-connect-knsid8`; make sure you're on
   that branch or have merged it.
4. `CLAUDE.md` is checked into the repo, so the local session inherits the
   risk rules, the sell policy, and the backtest findings automatically. It
   should read that file first.

## Prerequisites already verified on this machine (2026-08-07)

The owner ran `check_tws.bat` successfully, which proves all of:

- TWS API socket enabled (Global Configuration > API > Settings >
  "Enable ActiveX and Socket Clients")
- `127.0.0.1` in the trusted IPs list
- socket port matches (7496 live TWS; **4001** if you run IB Gateway)
- the venv has `ib_async` installed

So if a local script fails to connect, suspect the **one-login-per-username**
rule first: TWS, the phone app, and IB Gateway all use `alcademe67` and evict
each other. IB Gateway in particular has **no order-entry UI at all** — if
you're looking for a ticket and can't find one, check which app is actually
open.

## State as of 2026-08-12

- **Holding:** 0.2836 QQQ, cost basis $726.27/sh incl. commission
  ($205.97 total), bought 2026-08-07 @ $722.74.
- **Cash:** $87.21. **Net liquidation:** ~$292.
- **Deployed:** ~70% (ETF cap 70% of available funds, portfolio cap 80%).
- **Working orders:** none. No stop, no target, no sell trigger — by design.
- **Scan loop:** disabled (`SCAN_ENABLED` defaults false). Both strategies
  backtested to negative expectancy; don't turn it on without re-validating
  through `net_of_commission.py`.

## Scripts worth knowing

| script | what it does | needs TWS? |
|---|---|---|
| `check_tws.bat` → `examples/tws_status.py` | account + **all** working orders, read-only | yes |
| `examples/place_core_holding.py QQQ` | sizes off live cash, submits on typed YES | yes |
| `examples/cancel_open_orders.py` | lists working orders, dry run by default | yes |
| `examples/show_journal.py` | decision journal | no |

## Known gotchas (learned expensively — full detail in CLAUDE.md)

- `create_order_instruction` on the MCP connector **has never produced a
  fill.** Every executed order in this account was entered by hand in TWS.
- `get_account_orders` **under-reports** working orders. The TWS Orders tab
  and `tws_status.py` are authoritative.
- The TWS Order Entry panel **pre-fills a quantity**. Always check the value
  estimate next to SUBMIT (`≈ 205 USD`, not `≈ 72.2K USD`).
- Price buy limits **above** the ask and sell limits **below** the bid. The
  order fills at the market side either way, so the buffer is free and stops
  the order going un-marketable while you type.
- DAY orders outside 09:30–16:00 ET are worthless, and a limit priced off a
  16:00 quote is stale against the next open.
