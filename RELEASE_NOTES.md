# Release Candidate 1 — `v1.0.0-rc1`

**Status: FROZEN.** The next phase is **operational monitoring**, not development.

This tag marks the current code as Release Candidate 1. The trading logic is
considered feature-complete for evaluation. Do not add features or change the
strategy from here — see the change policy below.

## What's in RC1

- **Gated live trading on KuCoin** — real orders require `LIVE_TRADING=true`
  **and** the `bot.golive` operator confirmation token; paper mode otherwise.
- **Regime signal, per coin** — bull → trend-following (rides until a genuine
  trend break), sideways → mean reversion, high-vol → breakout, bear → cash.
- **Multi-coin watchlist** (`TRADE_SYMBOLS`) with global risk caps shared across
  all coins.
- **Risk controls** — 1% risk/trade, notional cap (`MAX_POSITION_PCT`), max open
  positions, daily-loss limit, daily-order cap, consecutive-loss pause, ATR
  stop-loss + profit target, breakeven/trailing options.
- **NET P/L accounting** — realized P/L is recorded after exchange fees; the
  daily-loss limit, stats, history, and win/loss all use net.
- **Pipeline instrumentation** — every entry decision and order is logged
  (`PIPE …`, `LIVE ORDER …`, `FILL …`, `OPEN/CLOSE …`).
- **Emergency stop** — a `STOP` file halts the engine on the next tick.
- Test suite: **68 passing**.

## Change policy while frozen

Only these categories may be changed in RC1:

1. **Crashes** (unhandled exceptions, restart/recovery failures)
2. **API errors** (KuCoin request/response handling, auth, retries)
3. **Accounting bugs** (P/L, fees, balances, sizing math)
4. **Exchange compatibility** (KuCoin symbol rules, order/fill formats, limits)
5. **Security issues** (secret handling, permissions, signing)

**Do NOT change** entries, exits, indicators, regime rules, thresholds, or any
"optimization" **unless real trading data demonstrates a reproducible problem** —
i.e. logged evidence from live/paper runs that a specific condition misbehaves,
with steps to reproduce. A backtest opinion or an idea is not sufficient grounds
to change frozen logic.

## Fixes since RC1

Allowed under the freeze (accounting / exchange only — no strategy change):

- **KuCoin `200004 "Balance insufficient"` on order submit.** Sizing to
  available cash targeted ~100% of the balance, so a market buy's real cost
  (notional + taker fee, filled at the ask) overran the balance. Fix: a
  configurable cash reserve (`ORDER_CASH_BUFFER`, default 2%) is held back when
  sizing, and `validate_buy` now requires `notional + fee ≤ available` before
  submitting — turning a would-be exchange rejection into a clean local skip.
  Funds are read from the **trade** account (correct for spot). Set
  `ORDER_CASH_BUFFER=0.10` to spend ≤90% of the balance.
- **Market BUYs now placed by `funds` (quote to spend), not `size` (base).** A
  size-based market buy makes KuCoin reserve funds against the order book and
  can overrun the balance (200004). A funds order spends at most the amount
  named, so it can't. The position records the ACTUAL filled base from the
  fills, conservatively reduced by one taker fee and floored to the lot size,
  so the later SELL never tries to offload more base than we hold. SELLs stay
  base-size. **Verify the first live buy→sell round-trip** (mock-tested only).

- **Phantom position on restart → SELL of coins not held (200004).** The engine
  recovered a position from SQLite that the exchange didn't actually hold, then
  a stop/target check fired a SELL for base it didn't own. Fixes: (1) startup
  **reconciliation** — each recovered position is compared to the exchange
  base-asset balance and removed (no P/L booked) if unbacked, with a logged
  reason; (2) `close_long` never submits a SELL unless the exchange confirms
  the base balance (else it removes the phantom, no order); (3) while
  `regime=warming-up` the engine submits **no orders** at all. `state.remove_position`
  deletes a phantom without recording a trade.

- **Stuck `regime=warming-up` → never trades.** `fetch_ohlcv` requested no time
  range, so KuCoin returned only a small recent slice of candles — fewer than
  the ~200 the trend EMA needs — so the regime never warmed up and no order was
  ever placed. Fix: request an explicit `[startAt, endAt]` window sized to the
  candle count (per-timeframe seconds table), guaranteeing enough history.

## Operating it

See `LIVE_TRADING.md` for setup, the go-live steps, and the risk-control
reference. Run in **paper mode first** and watch the `PIPE`/`FILL` logs; enable
live only deliberately, starting small.

> Automated trading can lose money. RC1's controls bound how much and how fast
> you can lose — they do not make the strategy profitable.
