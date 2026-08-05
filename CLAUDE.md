# Medik — IBKR trading automation

## Standing risk rules (set by the account owner, 2026-07-21)

These apply to every trade idea, order draft, or position plan produced in
this repo or via connected IBKR tools. They are enforced in code by
`strategy/config.py` + `strategy/risk.py` — do not bypass them.

1. **Max 20% of available funds per trade.** Never size a single position
   above 20% of the account's current available funds (not net liquidation).
1b. **Max 80% of the account deployed in total** (rule updated by the owner,
   2026-07-23). Positions may stack up to 80% of equity combined; at least
   20% of equity always stays in cash. Enforced via
   `strategy.risk.portfolio_headroom`.
2. **Minimum risk/reward** — 1:3 for the gate strategy (target derived from
   stop distance) or 1:2 for the pullback strategy, where the target is the
   *actual* recent swing-high resistance rather than a derived multiple
   (adopted 2026-08-04, see rule 8). Either way the target must clear its
   required R:R or the trade doesn't fire — `strategy.risk.size_position`
   enforces this whether the target is derived or explicitly given.
3. **Multi-indicator confirmation required (gate strategy).** A trade
   signal only counts if ALL of these agree on the same direction (see
   `strategy/signals.py`): EMA50/EMA200 trend, RSI(14) in the directional
   band, MACD(12,26,9) alignment, and volume >= its 20-day average.
4. **Score >= 70/100 to be tradeable (gate strategy only).** Every
   gate-passing candidate is ranked 0-100 by `strategy/scoring.py` (trend
   strength, momentum quality, volume surge, risk quality). Recalibrated
   from 90 on 2026-08-04 — backtested against 506 gate-passing bars across
   85 symbols over ~1 year, the max score ever achieved was 82.3, so 90 was
   unreachable and silently meant the system would never trade at all.
5. **Max 1% of net liquidation at risk per trade** (tightened from 2% on
   2026-08-04 — conventional swing-trading guidance for a small/developing
   account; see the provenance note in rule 8. 2% remains the hard ceiling
   this must never be raised above without the owner), in addition to the
   20% notional cap — whichever is tighter binds. Enforced in
   `strategy.risk.size_position` via its `net_liquidation` argument.
6. **Portfolio circuit breakers** (`strategy/risk_limits.py`): new entries
   halt automatically if the account hits its daily loss limit (3%), weekly
   drawdown limit (6%), monthly drawdown limit (10%), max concurrent
   positions (5), or the 80% exposure cap — checked before every scan via
   `check_trade_allowed`.
7. **Only completed session bars feed the gate.** `strategy/data_quality.py`
   drops any trailing daily bar whose US regular session hasn't closed yet
   (America/New_York wall-clock, not a UTC-hour proxy) — added after a
   stale mid-session bar produced a false gate failure on 2026-08-03.
8. **Second long-signal source: trend-pullback strategy** (`strategy/pullback.py`,
   added 2026-08-04). 200-SMA rising, pullback to the 8-EMA holding above the
   200-SMA, reclaim with a higher low. Stop below the pullback low; target
   the recent swing high (real resistance) at minimum 1:2, per rule 2 —
   evaluated independently of the gate strategy every scan, long-only. Both
   strategies' decisions are labeled `strategy=gate` / `strategy=pullback`
   in the journal so performance can be compared per-strategy.
   **PROVENANCE — read this before citing it.** The owner asked for this to
   be modeled on Humbled Trader (Shay Huang, humbledtrader.com). That site
   is **blocked from this environment (HTTP 403)** and was never read. These
   rules come from Claude's general knowledge of trend-pullback swing
   trading and are only *attributed* to that methodology — they have NOT
   been checked against her actual published material. Do not describe them
   as "her" rules or as verified from source. The same caveat applies to the
   1%-risk and 1:2-R:R choices in rules 2 and 5: sound, conventional swing
   practice, but not sourced.
   **VALIDATION STATUS: TESTED — DOES NOT HAVE A TRADEABLE EDGE.**
   See "Backtest verdict" below. Do not present pullback signals as
   validated; they are not.

## Backtest verdict (2026-08-05) — READ BEFORE PROPOSING ANY LIVE TRADE

Both strategies were backtested over **2 years of real IBKR daily bars,
201 symbols**, long-only, with the full risk engine and fractional sizing
active (`<scratchpad>/run_pullback_bt.py`, `net_of_commission.py`).

| | gross P&L | commissions | **net** |
|---|---|---|---|
| pullback (149 trades) | +$4.72 | −$112.47 | **−$107.75 (−35.9%)** |
| gate (45 trades) | +$6.73 | −$31.65 | **−$24.92 (−8.3%)** |

Two separate problems, and the second is the fatal one:

1. **Commissions.** This account's real schedule (measured from 90 days of
   actual fills) is `clamp($0.005/share, min $1.00, max 1% of trade value)`.
   At ~$35 positions the 1% cap binds both ways, so **every round trip costs
   ~2% of position value**. Average gross profit per pullback trade was
   ~$0.03 against ~$0.75 of commission.
2. **There is essentially no edge to begin with.** Gross profit factor was
   **1.03** (pullback) — +1.57% over *two years*, before costs. Scaling the
   account does not rescue it: gross scales linearly but commissions floor
   at $1.00/fill (~$298 for 298 fills), so **break-even is around a
   $19,000 account, at which point the strategy earns roughly $0** while
   an index fund returned far more over the same window.

**Therefore: do not recommend, draft, or place live trades from these
strategies as configured.** If the owner asks to trade them anyway, say
plainly that the tested expectancy is negative and why. Legitimate paths
forward are (a) find a genuinely higher-expectancy setup and re-test it the
same way, (b) trade far less frequently so commission drag matters less, or
(c) accept that a ~$300 account cannot overcome a 2%-per-round-trip cost
structure and treat this repo as research/paper-trading infrastructure.
Any future strategy change MUST be re-run through
`net_of_commission.py` before it is described as working.

## Execution policy

- **Claude never places live orders autonomously.** Orders are drafted
  (IBKR MCP `create_order_instruction`, which requires the owner to review
  and submit in the IBKR app) or proposed as `ibkr/orders.py` calls that
  require an explicit `confirm=True` from the person running them. This
  includes existing positions — analysis and recommendations yes,
  unilateral buying/selling/shorting no.
- Prefer limit orders over market orders on the live account.
- **Unattended/hands-free automation is allowed ONLY against the paper
  account** (`examples/autotrade_paper.py`: TWS paper port 7497, aborts
  unless every managed account id starts with "D"). The live account
  always keeps a human tap between signal and execution.
- The live TWS socket is 127.0.0.1:7496; scripts here only work while the
  owner's TWS is open and logged in.
- **`service/` (added 2026-08-03)** — a Windows background service
  (`supervisor.py`) that runs the scan/score/risk pipeline continuously,
  with reconnect-with-retry, crash recovery, health monitoring, and
  rotating logs. `MODE=PAPER` is fully autonomous, same guard as
  `autotrade_paper.py`. `MODE=LIVE` runs the full pipeline against the
  live account and QUEUES qualifying setups to `journal.pending_orders` —
  it does not call `ib.placeOrder`/modify/cancel under any mode setting;
  there is no code path in this service that submits a live order without
  a human running `examples/review_pending_orders.py` and typing YES per
  order. This was explicitly requested and explicitly declined during
  development — do not add one. See `service/SETUP_WINDOWS.md`.

## Account facts (verified, stable)

- **TFSA, base USD, LONG-ONLY** — cannot short. SELL/short signals from the
  gate are journaled but must never be traded or drafted.
- **Fractional shares are enabled** and are essential: the account is ~$300,
  so a 20%-of-funds slice is ~$28 and can't buy one whole share of most
  names. Always pass `fractional=True` to `size_position` for live sizing.
- **One IBKR login per username.** Desktop TWS and the phone app both use
  `alcademe67` and kick each other off. A second username (`alcademe6767`)
  has been pending IBKR identity verification since 2026-07-23 — check
  Client Portal → Message Center for a "Document Required" flag. Until it
  activates, TWS cannot stay connected while the phone app is open.
- Claude's IBKR MCP connector is a **separate session** from the owner's
  logins — querying the account never disconnects their TWS or phone.

## Environment gotchas (learned the hard way, 2026-08-03/05)

- **Cron jobs are session-only and get wiped constantly** — often within
  minutes, and always when the session ends. `CronList` before assuming any
  watch is armed; re-arm on every session start. There is no durable
  scheduler here. The reliable path is the Windows service in `service/`.
- **Egress blocks a lot of the web.** Confirmed 403: Yahoo Finance /
  yfinance, Wikipedia, humbledtrader.com. Market data must come from the
  IBKR connector. WebSearch works and is the tool for news vetoes.
- **Subagents must never call sleep/wait/Monitor** — foreground waiting is
  blocked and hangs the agent until it dies. Tell them explicitly to retry
  failed calls immediately with no pause.
- **The IBKR connector rate-limits under parallel load.** ~5 concurrent
  subagents is workable; 10 causes stalls and truncated/scrambled writes.
  Have agents work one ticker at a time (fetch → write → verify → next) and
  verify array-length consistency per file; batching many fetches before
  writing produces cross-contaminated data.
- **Long fan-outs hit session token limits** (resets are several hours
  apart). Check coverage and gap-fill rather than restarting a whole batch.

## Data caches (scratchpad, not in git)

- `<scratchpad>/data/*.json` — ~1-year daily bars, ~88 names.
- `<scratchpad>/data2y/*.json` — ~2-year daily bars, **~193 of 204** names,
  fetched 2026-08-05 for the pending pullback backtest. Known bad/missing:
  LUNR, REGN, TMO (ragged arrays — refetch before use); CFLT, BITF (no
  resolvable US listing); ALB, AAL, DAL, UAL, CCL, NCLH (not fetched).
  SNDK and WOLF have <2y of genuine history (spinoff/reorg) — not corrupt.
- `<scratchpad>/eval_both.py` — evaluates one symbol against BOTH strategies
  and enforces all sizing/R:R rules; used by the morning-scan cron.
- **Always validate a cache file before scoring**: all six OHLCV+time arrays
  must be equal length. A mid-session partial bar cached and reused after
  close silently produced a false gate failure on 2026-08-03 — that's why
  `strategy/data_quality.py` exists.

## Alerts

When a scan produces a qualifying signal during a Claude session, push it to
the owner's phone with the PushNotification tool: symbol, side, entry, stop,
target, quantity. One line, no fluff. Note the background Windows service
**cannot** reach the phone this way (no Claude session) — it falls back to a
Windows toast + `logs/alerts.log`, or SMTP if configured.

## Layout

- `ibkr/` — TWS connection (`client.py`), order helpers (`orders.py`),
  historical data (`data.py`), market scanner (`scanner.py`)
- `strategy/` — indicator math (`indicators.py`), signal gate
  (`signals.py`), trend-pullback strategy (`pullback.py`), candidate
  scoring (`scoring.py`), position sizing/risk (`risk.py`), portfolio
  circuit breakers (`risk_limits.py`), in-trade management:
  breakeven/trailing/partial-profit (`trade_management.py`),
  bar-completeness validation (`data_quality.py`), universe list
  (`universe.py`), config, and the SQLite decision journal (`journal.py`,
  → `journal.sqlite` at repo root, gitignored)
- `backtest/` — no-lookahead multi-symbol backtester (signal on close,
  fill at next open, stop-first when stop and target share a bar)
- `examples/` — runnable entry points (`connect_test.py`, `run_scan.py`,
  `run_backtest.py`, `manage_open_positions.py`, `check_risk_limits.py`,
  `show_journal.py`, `review_pending_orders.py`)
- `service/` — Windows background service: `supervisor.py` (main loop),
  `pipeline.py` (shared scan-score-risk-act cycle, mode-aware),
  `config.py`, `health.py`, `alerts.py`, `market_hours.py`,
  `logging_setup.py`, `run_supervisor.bat`, `SETUP_WINDOWS.md`
