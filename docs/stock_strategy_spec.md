# Autonomous Stock Decision Engine — Spec & Validation Plan

Status: **DESIGN — not built, not tested, NOT authorized for live.**
Owner asked (2026-09-14) for a real, tested stock strategy for individual
equities, kept fully separate from the ETF bot, live only after validation.

> **Honest prior — read first.** Three stock strategies already exist in this
> repo and ALL backtested negative: the gate (`strategy/signals.py`, −8.3%),
> the pullback (`strategy/pullback.py`, −35.9%), and the swing
> (`strategy/medik_swing.py`, −90.3%/5y). The dominant cause was **commission
> drag** — this account's real schedule is `clamp($0.005/sh, min $1.00, max 1%)`,
> so a small-size round trip costs ~2% of position value — plus **no measured
> gross edge**. Buy-and-hold beat every active strategy tested. This engine is
> therefore designed *around* those failure modes, and the honest expectation
> is that it is hard to beat costs at this account size. The backtest decides,
> not optimism.

---

## 1. Design principles (derived from why the prior ones failed)

1. **Trade rarely, hold long.** Commission drag is fatal to high-frequency at
   small size. This is a *position-trading* engine (holds days→months), not an
   intraday or short-swing one, so commissions are a small fraction of the move.
2. **Only trade strong, liquid trends.** Long-only, large-cap, high dollar
   volume — where trend persistence is most reliable and fills are clean.
3. **Cut fast, let winners run.** Asymmetric exits: tight structural stop,
   trend-following trail that stays in as long as the trend holds.
4. **Benchmark honestly.** The bar is not "positive" — it's "beats buy-and-hold
   net of costs, or matches its return at materially lower drawdown." If it only
   ties an index fund, it is not worth the risk and will not be promoted.

## 2. Universe
Liquid US large-cap common stocks (long-only, no leverage, no options).
Screened each run by: price > \$10, 20-day average dollar volume > \$50M.
Explicitly EXCLUDES the owner's manually-held names unless he adds them.

## 3. Entry rules (ALL must hold, evaluated on the daily close)
- **Regime:** last close > 200-day SMA **and** 50-SMA > 200-SMA.
- **Setup (one of):**
  - *Pullback-reclaim:* pulled back to within ~1 ATR of the 50-SMA, then a
    higher low and a close back above the 20-EMA; **or**
  - *Breakout:* close > the highest high of the last 60 sessions, on volume ≥
    1.5× its 20-day average.
- **Momentum quality:** RSI(14) between 45 and 80 (not exhausted); MACD
  (12,26,9) line above its signal.
- **Not extended:** close ≤ 1.10 × 50-SMA (don't chase a vertical move).

## 4. Sizing & risk (per position)
- **Risk budget:** 1.0% of equity per trade; hard ceiling 1.5%. (Same discipline
  as the ETF engine.)
- **Stop:** the greater of {2.5 × ATR(14) below entry, the pullback swing low}.
- **Size:** `risk_$ / (entry − stop)`, then capped by max-position notional
  (25% of equity) and available cash. Whole shares (fractional if enabled).
- **Real fills modeled in backtest:** next-open fill + half-spread + slippage;
  a gap through the stop fills at the open; commissions on both legs.

## 5. Hold / Reduce / Exit (evaluated daily)
- **HOLD** while close > 50-SMA and the trailing stop is not hit.
- **REDUCE** (optional, tested separately — it adds a commission): trim 1/3 and
  raise stop to breakeven if RSI(14) > 85 for 2 consecutive closes. Included in
  the backtest only if it improves net expectancy; dropped if it just adds cost.
- **EXIT** on the first of: stop hit; a close below the 50-SMA for 2 consecutive
  sessions; a 3 × ATR trailing stop; or a hard max-hold of 120 sessions.

## 6. Portfolio-level controls
- Max **3** concurrent positions; max **80%** of equity deployed (≥20% cash).
- At most **1 position per GICS sector** (avoid correlated stacking).
- Reuse `strategy/risk_limits.py` breakers: daily 3% / weekly 6% / monthly 10%
  drawdown halts on new entries.
- Real-time data mandatory for live entries (same `read_quote` gate as the ETF
  bot — delayed/frozen/stale data is refused). Completely separate process and
  code path from the ETF bot; neither can affect the other.

## 7. ACCEPTANCE CRITERIA — must ALL pass on OUT-OF-SAMPLE data before any live trade
Measured net of the real commission schedule, on a no-lookahead backtest
(signal on close, fill next open, stop-first), with an in-sample/out-of-sample
split where parameters are frozen before the OOS window is scored:

1. **OOS net profit factor ≥ 1.3**
2. **OOS net expectancy > 0** (positive dollars per trade after all costs)
3. **OOS max drawdown ≤ 25%**
4. **Beats SPY & QQQ buy-and-hold net of costs over the same window**, OR
   matches their return at materially lower drawdown
5. **≥ 30 OOS trades** (statistically meaningful) AND **commissions < 20% of
   gross profit** (proves it isn't just churning)
6. **Robust:** the edge holds in BOTH the full period and the OOS window; no
   retuning against OOS (that burns it — a fresh window would be required)

**If any gate fails → RED → NOT promoted. Reported honestly, shelved like the
prior three.** Only a clean pass on all six earns a live-integration proposal.

## 8. Build & validation process (the steps, in order)
1. `strategy/stock_engine.py` — pure decision functions (entry, sizing, exits,
   portfolio gates). Fully unit-tested.
2. `backtest/stock_bt.py` — no-lookahead multi-symbol harness reusing
   `backtest/engine.py` + `net_of_commission.py`.
3. Fetch real daily bars for the universe (IBKR, ~3–5 years incl. the 2022 bear)
   — a genuine data step, not cached assumptions.
4. Run full-period + in-sample(60%)/out-of-sample(40%).
5. Score against §7. Report every number honestly.
6. **Only on a clean pass:** present the acceptance results, then wire a
   separate live runner under the same standing authorization, with its own
   single-instance lock, STOP switch, and real-time-data gate.

## 9. What this does NOT do
- Does not touch the ETF bot or its config.
- Does not trade PATH/LULU (or anything) live until §7 passes.
- Does not get "made to pass" by tuning against the test window. If it's RED,
  it's RED — the honest outcome is the deliverable, not a forced green.
