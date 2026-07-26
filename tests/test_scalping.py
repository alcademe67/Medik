"""Tests for the fixed-% exit mode (scalping) in the backtester."""

import numpy as np
import pandas as pd

from trading import backtest, config
from trading.config import StrategyParams


def _oscillating_df(n=800, seed=1):
    t = np.arange(n)
    close = np.abs(100 + 12 * np.sin(t / 18) + np.random.default_rng(seed).normal(0, 0.8, n)) + 1
    return pd.DataFrame(
        {
            "open": close, "high": close + 0.4, "low": close - 0.4, "close": close,
            "volume": np.abs(np.random.default_rng(seed + 1).normal(1000, 100, n)),
        },
        index=pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC"),
    )


def test_fixed_mode_exits_at_the_configured_target(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY", "meanrev")
    params = StrategyParams(
        meanrev_trend_filter=False, exit_mode="fixed",
        profit_target_pct=0.5, stop_loss_pct=0.8,
    )
    res = backtest.run(_oscillating_df(), params, timeframe="1h", fee_rate=0.001)

    target_exits = [t for t in res.trades if t.reason == "target"]
    stop_exits = [t for t in res.trades if t.reason == "stop"]
    assert target_exits, "expected at least one take-profit exit"
    # A target exit closes exactly +0.5% gross; a stop exit exactly -0.8%.
    for t in target_exits:
        assert abs(t.return_pct - 0.5) < 1e-6
    for t in stop_exits:
        assert abs(t.return_pct + 0.8) < 1e-6


def test_fixed_mode_needs_no_atr(monkeypatch):
    # Fixed mode must still enter even where ATR-based sizing would be skipped.
    monkeypatch.setattr(config, "STRATEGY", "meanrev")
    params = StrategyParams(meanrev_trend_filter=False, exit_mode="fixed")
    res = backtest.run(_oscillating_df(), params, timeframe="1h", fee_rate=0.001)
    assert res.num_trades > 0
