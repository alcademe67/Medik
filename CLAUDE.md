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
2. **Minimum 1:3 risk/reward.** Reject any setup whose target is less than
   3x the stop distance from entry.
3. **Multi-indicator confirmation required.** A trade signal only counts if
   ALL of these agree on the same direction (see `strategy/signals.py`):
   EMA50/EMA200 trend, RSI(14) in the directional band, MACD(12,26,9)
   alignment, and volume >= its 20-day average.
4. **Score >= 90/100 to be tradeable** (added 2026-08-03). Every
   gate-passing candidate is ranked 0-100 by `strategy/scoring.py` (trend
   strength, momentum quality, volume surge, risk quality). Passing the
   4-indicator gate is necessary but not sufficient — a valid-but-weak
   setup below the threshold is not drafted.
5. **Max 2% of net liquidation at risk per trade**, in addition to the 20%
   notional cap — whichever is tighter binds. Enforced in
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

## Alerts

When a scan produces a qualifying signal during a Claude session, push it to
the owner's phone with the PushNotification tool: symbol, side, entry, stop,
target, quantity. One line, no fluff.

## Layout

- `ibkr/` — TWS connection (`client.py`), order helpers (`orders.py`),
  historical data (`data.py`), market scanner (`scanner.py`)
- `strategy/` — indicator math (`indicators.py`), signal gate
  (`signals.py`), candidate scoring (`scoring.py`), position sizing/risk
  (`risk.py`), portfolio circuit breakers (`risk_limits.py`), in-trade
  management: breakeven/trailing/partial-profit (`trade_management.py`),
  bar-completeness validation (`data_quality.py`), config, and the SQLite
  decision journal (`journal.py`, → `journal.sqlite` at repo root, gitignored)
- `backtest/` — no-lookahead multi-symbol backtester (signal on close,
  fill at next open, stop-first when stop and target share a bar)
- `examples/` — runnable entry points (`connect_test.py`, `run_scan.py`,
  `run_backtest.py`, `manage_open_positions.py`, `check_risk_limits.py`,
  `show_journal.py`, `review_pending_orders.py`)
- `service/` — Windows background service: `supervisor.py` (main loop),
  `pipeline.py` (shared scan-score-risk-act cycle, mode-aware),
  `config.py`, `health.py`, `alerts.py`, `market_hours.py`,
  `logging_setup.py`, `run_supervisor.bat`, `SETUP_WINDOWS.md`
