"""Multi-confirmation, long-only strategy template.

⚠️ HONEST NOTE: this is a *starting template*, not a proven money-maker.
It encodes the common "several indicators must agree" idea (EMA trend +
crossover, RSI, MACD, ADX, volume, Bollinger). Combining indicators does
NOT create an edge by itself — it usually overfits. The point of Phase 1
is to let you BACKTEST ideas like this and find out whether any of them
actually has an edge after fees. Treat a good backtest as "worth paper
trading", never as a guarantee.

`generate_signals` returns the OHLCV frame plus boolean `entry`/`exit`
columns. It is pure — the backtester turns those into simulated trades.
"""

from __future__ import annotations

import pandas as pd

from trading import indicators
from trading.config import DEFAULT_PARAMS, StrategyParams


def generate_signals(df: pd.DataFrame, params: StrategyParams = DEFAULT_PARAMS) -> pd.DataFrame:
    """Add indicator columns and boolean `entry` / `exit` signals.

    Long-only (spot). Entry requires ALL confirmations to hold on the bar
    (the backtester only acts on them while flat, so you enter on the first
    aligned bar, not every one):
      * uptrend        — close above the long trend EMA
      * momentum       — fast EMA above slow EMA (post-crossover regime)
      * RSI healthy    — above rsi_min_long but below rsi_max (not overbought)
      * MACD positive  — histogram above zero
      * real trend     — ADX above adx_min (filters chop / pump-and-dump noise)
      * volume         — bar volume above its moving average
    Exit on ANY of: fast crosses back below slow, MACD histogram turns
    negative, or RSI runs above rsi_max. (Stops/targets are applied by the
    backtester using ATR — that is the risk layer.)
    """
    out = indicators.add_indicators(df, params)

    cross_down = (out["ema_fast"] < out["ema_slow"]) & (
        out["ema_fast"].shift(1) >= out["ema_slow"].shift(1)
    )

    uptrend = out["close"] > out["ema_trend"]
    momentum = out["ema_fast"] > out["ema_slow"]
    rsi_ok = (out["rsi"] > params.rsi_min_long) & (out["rsi"] < params.rsi_max)
    macd_ok = out["macd_hist"] > 0
    trend_strong = out["adx"] > params.adx_min
    volume_ok = out["volume"] > out["vol_ma"]

    out["entry"] = (
        uptrend & momentum & rsi_ok & macd_ok & trend_strong & volume_ok
    ).fillna(False)
    out["exit"] = (
        cross_down | (out["macd_hist"] < 0) | (out["rsi"] > params.rsi_max)
    ).fillna(False)
    return out
