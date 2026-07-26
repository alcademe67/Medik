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

## Operating it

See `LIVE_TRADING.md` for setup, the go-live steps, and the risk-control
reference. Run in **paper mode first** and watch the `PIPE`/`FILL` logs; enable
live only deliberately, starting small.

> Automated trading can lose money. RC1's controls bound how much and how fast
> you can lose — they do not make the strategy profitable.
