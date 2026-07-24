"""Technical indicators, implemented in pure pandas/numpy.

Hand-rolled on purpose: no TA-Lib native dependency (a pain to install,
especially on Windows), everything is transparent, and every function is
unit-tested. Wilder-smoothed indicators (RSI, ATR, ADX) use an EWM with
alpha = 1/period, which is exactly Wilder's smoothing.

Each function takes/returns pandas Series (or a small DataFrame) so they
compose cleanly and never mutate their inputs.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI, bounded [0, 100]."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    out = 100 - 100 / (1 + rs)
    # Edge cases where rs is 0/0 or x/0:
    out = out.where(avg_loss != 0, 100.0)                 # no losses -> 100
    out = out.mask((avg_gain == 0) & (avg_loss == 0), 50.0)  # perfectly flat -> neutral
    return out


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """Return a DataFrame with macd line, signal line, and histogram."""
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "macd_signal": signal_line, "macd_hist": hist})


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range (Wilder-smoothed)."""
    return true_range(high, low, close).ewm(alpha=1 / period, adjust=False).mean()


def adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.DataFrame:
    """Wilder's ADX with +DI / -DI, all bounded [0, 100].

    ADX measures trend *strength* (not direction); +DI/-DI give direction.
    """
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    plus_dm = pd.Series(plus_dm, index=high.index)
    minus_dm = pd.Series(minus_dm, index=high.index)

    tr = true_range(high, low, close)
    atr_ = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx_ = dx.ewm(alpha=1 / period, adjust=False).mean()
    return pd.DataFrame({"adx": adx_, "plus_di": plus_di, "minus_di": minus_di})


def bollinger(
    close: pd.Series, period: int = 20, num_std: float = 2.0
) -> pd.DataFrame:
    """Bollinger Bands: middle (SMA), upper, lower, and %B position."""
    mid = sma(close, period)
    std = close.rolling(period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    width = (upper - lower).replace(0, np.nan)
    pct_b = (close - lower) / width
    return pd.DataFrame({"bb_mid": mid, "bb_upper": upper, "bb_lower": lower, "bb_pct": pct_b})


def add_indicators(df: pd.DataFrame, params) -> pd.DataFrame:
    """Return a copy of an OHLCV frame with every indicator column added.

    `df` must have columns: open, high, low, close, volume.
    """
    out = df.copy()
    out["ema_fast"] = ema(out["close"], params.ema_fast)
    out["ema_slow"] = ema(out["close"], params.ema_slow)
    out["ema_trend"] = ema(out["close"], params.ema_trend)
    out["rsi"] = rsi(out["close"], params.rsi_period)
    out = out.join(macd(out["close"], params.macd_fast, params.macd_slow, params.macd_signal))
    out["atr"] = atr(out["high"], out["low"], out["close"], params.atr_period)
    out = out.join(adx(out["high"], out["low"], out["close"], params.adx_period))
    out = out.join(bollinger(out["close"], params.bb_period, params.bb_std))
    out["vol_ma"] = sma(out["volume"], params.volume_ma)
    return out
