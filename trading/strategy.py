"""Trading strategies. Pick one with STRATEGY in .env:

  * "trend"   — momentum / trend-following (buy strength, ride it)
  * "meanrev" — buy the dip, sell high (mean reversion)

⚠️ HONEST NOTE: both are *templates*, not proven money-makers. The whole
point of the backtester is to find out whether either has an edge on real
data after fees. A good backtest means "worth paper-trading", never a
guarantee.

Each function returns the OHLCV frame plus boolean `entry`/`exit` columns.
They are pure — the backtester turns those into simulated trades.
"""

from __future__ import annotations

import pandas as pd

from trading import config, indicators
from trading.config import DEFAULT_PARAMS, StrategyParams


def _trend_signals(df: pd.DataFrame, params: StrategyParams) -> pd.DataFrame:
    """Momentum: enter when trend + momentum + RSI/MACD/ADX/volume all agree."""
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


def _mean_reversion_signals(df: pd.DataFrame, params: StrategyParams) -> pd.DataFrame:
    """Buy the dip, sell high.

    Entry: a dip that is *starting to reverse* — the previous bar was
    oversold (RSI below `rsi_oversold`) and closed below the lower Bollinger
    band, and this bar has closed back above the band. Waiting for the
    close back inside the band avoids buying while price is still falling
    (catching a "falling knife"). With the trend filter on (default), only
    dips *above* the long trend EMA qualify — pullbacks in an uptrend.

    Exit: price reverts back up — close returns to/above the Bollinger
    middle band (the mean), or RSI runs above `rsi_overbought`. (The
    backtester's ATR stop still caps the downside on each trade.)
    """
    out = indicators.add_indicators(df, params)

    was_oversold = out["rsi"].shift(1) < params.rsi_oversold
    below_band = out["close"].shift(1) < out["bb_lower"].shift(1)
    back_inside = out["close"] >= out["bb_lower"]
    bounce = was_oversold & below_band & back_inside   # the dip is turning up
    if params.meanrev_trend_filter:
        bounce = bounce & (out["close"] > out["ema_trend"])

    out["entry"] = bounce.fillna(False)
    out["exit"] = (
        (out["close"] >= out["bb_mid"]) | (out["rsi"] > params.rsi_overbought)
    ).fillna(False)
    return out


def generate_signals(df: pd.DataFrame, params: StrategyParams = DEFAULT_PARAMS) -> pd.DataFrame:
    """Add indicators + boolean `entry`/`exit`, dispatching on STRATEGY."""
    if config.STRATEGY == "meanrev":
        return _mean_reversion_signals(df, params)
    return _trend_signals(df, params)
