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

import dataclasses

import pandas as pd

from trading import config, indicators, regime
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


def _breakout_signals(df: pd.DataFrame, params: StrategyParams) -> pd.DataFrame:
    """High-volatility breakout: buy when price closes above the prior
    N-bar high on above-average volume; exit when it closes back below the
    prior N-bar low, or gets overbought."""
    out = indicators.add_indicators(df, params)
    prior_high = out["high"].rolling(params.breakout_lookback).max().shift(1)
    prior_low = out["low"].rolling(params.breakout_lookback).min().shift(1)
    out["entry"] = ((out["close"] > prior_high) & (out["volume"] > out["vol_ma"])).fillna(False)
    out["exit"] = ((out["close"] < prior_low) | (out["rsi"] > params.rsi_max)).fillna(False)
    return out


def _regime_signals(df: pd.DataFrame, params: StrategyParams) -> pd.DataFrame:
    """Regime-switching framework: each bar uses the entry rule matching its
    regime —

      * bull     -> trend-following (momentum: ride strength)
      * sideways -> mean reversion (buy the dip, sell the rip)
      * high_vol -> breakout
      * bear     -> cash (no entries)

    Exits are the union of the component exits; the stop/target still caps
    each trade."""
    reg = regime.detect_regime(df, params)
    trend = _trend_signals(df, params)
    mrev = _mean_reversion_signals(df, dataclasses.replace(params, meanrev_trend_filter=False))
    brk = _breakout_signals(df, params)

    entry = pd.Series(False, index=df.index)
    entry = entry.mask(reg == "bull", trend["entry"])
    entry = entry.mask(reg == "sideways", mrev["entry"])
    entry = entry.mask(reg == "high_vol", brk["entry"])
    # "bear" -> stay in cash, no entries.

    # The EXIT must match the regime too. A union of all exits would let the
    # mean-reversion exit (close >= mid-band, true on most up-trend bars) close
    # a trend entry almost immediately — trend-following could never ride a
    # move. So each regime exits on its OWN rule; a bear bar exits to cash.
    exit_ = pd.Series(False, index=df.index)
    exit_ = exit_.mask(reg == "bull", trend["exit"])
    exit_ = exit_.mask(reg == "sideways", mrev["exit"])
    exit_ = exit_.mask(reg == "high_vol", brk["exit"])
    exit_ = exit_.mask(reg == "bear", True)

    out = brk.copy()  # already carries the indicator columns
    out["entry"] = entry.fillna(False).astype(bool)
    out["exit"] = exit_.fillna(False).astype(bool)
    out["regime"] = reg
    return out


def regime_signals(df: pd.DataFrame, params: StrategyParams = DEFAULT_PARAMS) -> pd.DataFrame:
    """Public entry point for the regime framework — same as selecting
    STRATEGY=regime, but callable directly (the live engine uses this for its
    signal, bypassing the env dispatch)."""
    return _regime_signals(df, params)


def generate_signals(df: pd.DataFrame, params: StrategyParams = DEFAULT_PARAMS) -> pd.DataFrame:
    """Add indicators + boolean `entry`/`exit`, dispatching on STRATEGY."""
    strat = config.STRATEGY
    if strat == "meanrev":
        return _mean_reversion_signals(df, params)
    if strat == "breakout":
        return _breakout_signals(df, params)
    if strat == "regime":
        return _regime_signals(df, params)
    return _trend_signals(df, params)
