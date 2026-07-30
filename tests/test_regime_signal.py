"""Tests for the live regime-signal bridge (bot/regime_signal.py).

Verifies the OHLCV-frame builder and the mapping of the latest regime bar to
the engine's Signal enum. The regime maths itself is tested in test_regime.py;
here we pin the adapter that the live engine depends on.
"""

import numpy as np
import pandas as pd

from bot import regime_signal
from bot.strategy import Signal
from trading.config import StrategyParams


def _frame(n=10):
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    price = np.linspace(100, 110, n)
    return pd.DataFrame(
        {"open": price, "high": price + 1, "low": price - 1, "close": price, "volume": 1000.0},
        index=idx,
    )


def test_frame_from_ohlcv_builds_expected_columns():
    rows = [
        {"time": 1_600_000_000 + i * 3600, "open": 1.0, "high": 2.0,
         "low": 0.5, "close": 1.5, "volume": 10.0}
        for i in range(5)
    ]
    df = regime_signal.frame_from_ohlcv(rows)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 5


def test_frame_from_ohlcv_empty_is_safe():
    df = regime_signal.frame_from_ohlcv([])
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.empty


def test_signal_is_warming_up_until_trend_ema_is_ready():
    sig, reg = regime_signal.signal_from_frame(_frame(5), StrategyParams(ema_trend=200))
    assert sig == Signal.HOLD and reg == "warming-up"


def _patch_regime(monkeypatch, *, entry_last, exit_last, regime="bull"):
    def fake_regime_signals(df, params):
        out = df.copy()
        out["entry"] = False
        out["exit"] = False
        out["regime"] = regime
        out.iloc[-1, out.columns.get_loc("entry")] = entry_last
        out.iloc[-1, out.columns.get_loc("exit")] = exit_last
        return out
    monkeypatch.setattr(regime_signal.research, "regime_signals", fake_regime_signals)


def test_entry_on_last_bar_maps_to_buy(monkeypatch):
    _patch_regime(monkeypatch, entry_last=True, exit_last=False, regime="bull")
    sig, reg = regime_signal.signal_from_frame(_frame(60), StrategyParams(ema_trend=5))
    assert sig == Signal.BUY and reg == "bull"


def test_exit_on_last_bar_maps_to_sell(monkeypatch):
    _patch_regime(monkeypatch, entry_last=False, exit_last=True, regime="sideways")
    sig, reg = regime_signal.signal_from_frame(_frame(60), StrategyParams(ema_trend=5))
    assert sig == Signal.SELL and reg == "sideways"


def test_neither_maps_to_hold(monkeypatch):
    _patch_regime(monkeypatch, entry_last=False, exit_last=False, regime="bear")
    sig, reg = regime_signal.signal_from_frame(_frame(60), StrategyParams(ema_trend=5))
    assert sig == Signal.HOLD and reg == "bear"
