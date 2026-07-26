"""Tests for the mean-reversion ('buy the dip, sell high') strategy.

These check the signals are mechanically correct (clean booleans, buys
fire on dips-that-bounce). They do NOT assert profitability — whether the
strategy makes money is the backtester's job, on real data.
"""

import numpy as np
import pandas as pd

from trading.config import StrategyParams
from trading.strategy import _mean_reversion_signals


def _oscillating_df(n=800, seed=1):
    t = np.arange(n)
    # Keep this a numpy array: wrapping in a Series with a default index and
    # then handing it to a DataFrame with a datetime index misaligns them.
    close = np.abs(100 + 12 * np.sin(t / 18) + np.random.default_rng(seed).normal(0, 0.8, n)) + 1
    return pd.DataFrame(
        {
            "open": close, "high": close + 0.3, "low": close - 0.3,
            "close": close, "volume": [1000.0] * n,
        },
        index=pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC"),
    )


def test_mean_reversion_signals_are_clean_booleans():
    out = _mean_reversion_signals(_oscillating_df(), StrategyParams(meanrev_trend_filter=False))
    assert out["entry"].dtype == bool and out["exit"].dtype == bool
    assert not out["entry"].isna().any() and not out["exit"].isna().any()


def test_mean_reversion_fires_buys_and_sells_on_oscillation():
    out = _mean_reversion_signals(_oscillating_df(), StrategyParams(meanrev_trend_filter=False))
    assert out["entry"].any()   # dips-that-bounce trigger buys
    assert out["exit"].any()    # recoveries trigger exits
