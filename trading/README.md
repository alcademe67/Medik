# Trading research engine — Phase 1

A read-only toolkit for researching KuCoin strategies: pull market data,
scan/rank USDT pairs, compute indicators, and **backtest with real
performance metrics**. 

> **Phase 1 places no orders and needs no API keys.** It exists so you can
> answer the only question that matters before risking money: *does this
> strategy actually have an edge?* Live execution is a later, deliberately
> gated phase (see the roadmap below).

## Install

```bash
pip install -r requirements.txt      # adds ccxt, pandas, numpy
```

## Use it (all read-only)

```bash
# 1. Scan every liquid USDT pair, ranked by 24h volume
python -m trading scan

# 2. Backtest the strategy on one pair
python -m trading backtest BTC/USDT 1h

# 3. Backtest the top scanner hits and rank them by Sharpe
python -m trading backtest-top 1h
```

Example backtest output:

```
Backtest — BTC/USDT @ 1h  (500 bars)
----------------------------------------------
Trades          : 18
Win rate        : 50.0%
Total return    : +6.54%
CAGR            : +152.07%
Sharpe          : 3.62
Max drawdown    : -5.00%
Profit factor   : 2.18
----------------------------------------------
Research only — optimistic fills, past != future.
```

## Modules

| Module | Responsibility |
|--------|----------------|
| `config.py` | All settings + the `StrategyParams` knobs, from `.env` |
| `exchange.py` | CCXT/KuCoin wrapper — **market data only, no orders** |
| `indicators.py` | EMA, RSI, MACD, ATR, ADX, Bollinger (pure pandas, tested) |
| `scanner.py` | Filter illiquid coins, rank USDT pairs by volume |
| `strategy.py` | Multi-confirmation long-only signal template |
| `backtest.py` | Event-driven backtest + CAGR/Sharpe/maxDD/profit-factor |

## Two strategies to test

Pick one with `STRATEGY` in `.env`, then backtest it:

- **`STRATEGY=trend`** (default) — momentum: buy strength and ride the trend.
- **`STRATEGY=meanrev`** — "buy the dip, sell high": buy when price is
  oversold and bounces back above the lower Bollinger band, sell when it
  reverts to the mean. Tune with `RSI_OVERSOLD`, `RSI_OVERBOUGHT`,
  `MEANREV_TREND_FILTER`.

Both are *templates*. Backtest them (`python -m trading backtest-top 4h`)
and let the numbers decide — neither is a guaranteed money-maker.

## Tuning

Every strategy knob lives in `StrategyParams` (`config.py`) and is
overridable from `.env` — RSI period, EMA lengths, ATR stop multiplier,
risk/reward, ADX minimum, etc. Change them and re-run `backtest` to see
the effect. Automated parameter sweeps are the "optimization" phase.

## An honest word on the strategy

The built-in strategy ("enter when EMA trend + RSI + MACD + ADX + volume
all agree") is a **template, not a proven money-maker**. Stacking
indicators does not create an edge — it usually overfits the past. Use
this engine to *test* ideas and be skeptical: a good backtest means "maybe
worth paper-trading", never "this will make money". Fills are modelled
optimistically (close-fills, stop-before-target), so live results are
worse.

## Roadmap (later, gated phases)

Phase 1 is the foundation. The rest of the requested system builds on it,
with live trading **off by default** the whole way:

- **Phase 2 — Risk & paper execution:** position sizing (≤1% risk/trade),
  daily-loss cap, max open positions, ATR stops, trailing stops — running
  in **paper mode** (simulated fills, no real orders).
- **Phase 3 — Live execution (gated):** real KuCoin orders behind a
  `LIVE_TRADING` switch that stays off until a strategy is validated;
  retries, reconnects, duplicate-order guards, restart recovery.
- **Phase 4 — Ops:** FastAPI dashboard, PostgreSQL, Redis, APScheduler,
  Telegram alerts, Docker Compose, health checks, cloud deploy.

Each phase is added only after the previous one is tested — that's what
keeps a money-touching system safe.
