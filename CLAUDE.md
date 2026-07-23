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

## Alerts

When a scan produces a qualifying signal during a Claude session, push it to
the owner's phone with the PushNotification tool: symbol, side, entry, stop,
target, quantity. One line, no fluff.

## Layout

- `ibkr/` — TWS connection (`client.py`), order helpers (`orders.py`),
  historical data (`data.py`), market scanner (`scanner.py`)
- `strategy/` — indicator math, signal gate, risk sizing, config
- `backtest/` — no-lookahead multi-symbol backtester (signal on close,
  fill at next open, stop-first when stop and target share a bar)
- `examples/` — runnable entry points (`connect_test.py`, `run_scan.py`,
  `run_backtest.py`)
