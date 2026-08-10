"""Combines the indicator stack into a single go/no-go trade signal.

A trade only fires when trend, momentum, and volume all agree on the same
direction. Any one of them disagreeing kills the signal for that bar.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from strategy import indicators as ind
from strategy.config import StrategyConfig, DEFAULT_CONFIG

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class SignalResult:
    side: str | None  # "BUY", "SELL", or None if no signal
    entry: float | None
    stop: float | None
    checks: dict = field(default_factory=dict)
    passed: bool = False
    reason: str = ""


def compute_indicator_frame(df: pd.DataFrame, config: StrategyConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"missing required columns: {missing}")

    out = df.copy()
    out["ema_fast"] = ind.ema(out["close"], config.ema_fast)
    out["ema_slow"] = ind.ema(out["close"], config.ema_slow)
    out["rsi"] = ind.rsi(out["close"], config.rsi_period)
    macd_df = ind.macd(out["close"], config.macd_fast, config.macd_slow, config.macd_signal)
    out["macd"] = macd_df["macd"]
    out["macd_signal"] = macd_df["signal"]
    out["macd_hist"] = macd_df["hist"]
    out["atr"] = ind.atr(out["high"], out["low"], out["close"], config.atr_period)
    out["volume_sma"] = ind.volume_sma(out["volume"], config.volume_sma_period)
    return out


def evaluate(df: pd.DataFrame, config: StrategyConfig = DEFAULT_CONFIG) -> SignalResult:
    """Evaluate the last row of an OHLCV-plus-indicators frame (as produced by
    compute_indicator_frame) for a trade signal. df must already have at
    least config.warmup_bars rows of history before the row being evaluated.
    """
    if len(df) < config.warmup_bars:
        return SignalResult(None, None, None, {}, False, "insufficient history for warm-up")
    return evaluate_row(df.iloc[-1], config)


def evaluate_row(row: pd.Series, config: StrategyConfig = DEFAULT_CONFIG) -> SignalResult:
    """Same as evaluate(), but takes a single already-indicator-computed row.
    Lets a caller iterating over a precomputed indicator frame (e.g. the
    backtester) skip re-slicing a DataFrame on every bar.
    """
    if row[["ema_fast", "ema_slow", "rsi", "macd", "macd_signal", "atr", "volume_sma"]].isna().any():
        return SignalResult(None, None, None, {}, False, "indicators not yet valid (NaN)")

    entry = float(row["close"])
    atr_val = float(row["atr"])
    if atr_val <= 0:
        return SignalResult(None, None, None, {}, False, "zero/invalid ATR")

    trend_up = row["ema_fast"] > row["ema_slow"]
    trend_down = row["ema_fast"] < row["ema_slow"]
    volume_confirmed = row["volume"] >= config.volume_min_ratio * row["volume_sma"]

    checks_long = {
        "trend_up": bool(trend_up),
        "rsi_in_range": bool(config.rsi_buy_min <= row["rsi"] <= config.rsi_buy_max),
        "macd_bullish": bool(row["macd"] > row["macd_signal"] and row["macd_hist"] > 0),
        "volume_confirmed": bool(volume_confirmed),
    }
    checks_short = {
        "trend_down": bool(trend_down),
        "rsi_in_range": bool(config.rsi_sell_min <= row["rsi"] <= config.rsi_sell_max),
        "macd_bearish": bool(row["macd"] < row["macd_signal"] and row["macd_hist"] < 0),
        "volume_confirmed": bool(volume_confirmed),
    }

    if all(checks_long.values()):
        stop = entry - config.atr_stop_mult * atr_val
        return SignalResult("BUY", entry, stop, checks_long, True, "all long checks passed")
    if all(checks_short.values()):
        stop = entry + config.atr_stop_mult * atr_val
        return SignalResult("SELL", entry, stop, checks_short, True, "all short checks passed")

    failed_long = [k for k, v in checks_long.items() if not v]
    return SignalResult(
        None, entry, None,
        {"long": checks_long, "short": checks_short}, False,
        f"no consensus (long failed: {failed_long})",
    )
