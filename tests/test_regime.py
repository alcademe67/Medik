"""Tests for regime detection, the breakout strategy, and the dispatcher."""

import dataclasses

import numpy as np
import pandas as pd

from trading.config import StrategyParams
from trading.regime import detect_regime
from trading.strategy import (
    _breakout_signals,
    _mean_reversion_signals,
    _regime_signals,
    _trend_signals,
)


def _df(closes, volume=1000.0):
    close = np.asarray(closes, dtype=float)
    vol = volume if np.ndim(volume) else [volume] * len(close)
    return pd.DataFrame(
        {"open": close, "high": close + 0.3, "low": close - 0.3, "close": close, "volume": vol},
        index=pd.date_range("2024-01-01", periods=len(close), freq="h", tz="UTC"),
    )


def test_detect_regime_bull_and_bear():
    reg_up = detect_regime(_df(np.linspace(50, 150, 300)))     # steady uptrend
    reg_dn = detect_regime(_df(np.linspace(150, 50, 300)))     # steady downtrend
    assert set(reg_up.unique()) <= {"bull", "bear", "sideways", "high_vol"}
    assert (reg_up.iloc[260:] == "bull").mean() > 0.5
    assert (reg_dn.iloc[260:] == "bear").mean() > 0.5


def test_breakout_fires_on_new_high_with_volume():
    closes = [100.0] * 40 + [106.0]           # jump to a fresh high
    vols = [1000.0] * 40 + [3000.0]           # on a volume spike
    out = _breakout_signals(_df(closes, np.array(vols)), StrategyParams())
    assert bool(out["entry"].iloc[-1])        # the breakout bar triggers a buy


def test_regime_signals_are_clean_and_labelled():
    close = 100 + 10 * np.sin(np.arange(400) / 15) + np.linspace(0, 20, 400)
    out = _regime_signals(_df(close), StrategyParams())
    assert out["entry"].dtype == bool and out["exit"].dtype == bool
    assert "regime" in out.columns
    assert not out["entry"].isna().any() and not out["exit"].isna().any()
    # bear bars must never carry an entry (spot framework stays in cash).
    assert not out.loc[out["regime"] == "bear", "entry"].any()


def test_regime_routes_bull_to_trend_following():
    # Per spec, bull regime uses the trend/momentum entries (not mean reversion).
    close = 100 + np.linspace(0, 60, 400) + 3 * np.sin(np.arange(400) / 8)
    df = _df(close)
    p = StrategyParams()
    out = _regime_signals(df, p)
    trend = _trend_signals(df, p)
    bull = out["regime"] == "bull"
    # On every bull bar, the framework's entry must equal the trend strategy's.
    assert (out.loc[bull, "entry"] == trend.loc[bull, "entry"]).all()


def test_regime_bull_exit_only_on_trend_break():
    # Bull exits ONLY on a genuine trend break: fast EMA below slow EMA, or
    # close below the slow EMA — never on overbought RSI / soft MACD.
    close = 100 + np.linspace(0, 60, 400) + 3 * np.sin(np.arange(400) / 8)
    df = _df(close)
    p = StrategyParams()
    out = _regime_signals(df, p)
    trend = _trend_signals(df, p)
    bull = out["regime"] == "bull"
    trend_break = (trend["ema_fast"] < trend["ema_slow"]) | (trend["close"] < trend["ema_slow"])
    assert (out.loc[bull, "exit"] == trend_break[bull]).all()


def test_regime_bull_holds_through_overbought():
    # The point of the change: while the trend is intact, an overbought RSI
    # (> rsi_max) must NOT trigger an exit (the old trend exit would have).
    close = np.linspace(100, 300, 400) + 2 * np.sin(np.arange(400) / 8)   # steep climb
    df = _df(close)
    p = StrategyParams()
    out = _regime_signals(df, p)
    trend = _trend_signals(df, p)
    bull = out["regime"] == "bull"
    intact = (trend["ema_fast"] >= trend["ema_slow"]) & (trend["close"] >= trend["ema_slow"])
    overbought = trend["rsi"] > p.rsi_max
    held = bull & intact & overbought
    assert held.any()                          # the scenario really occurs here
    assert not out.loc[held, "exit"].any()     # ...and we hold through it


def test_regime_sideways_and_bear_exits_unchanged():
    close = 100 + 10 * np.sin(np.arange(400) / 15) + np.linspace(0, 20, 400)
    df = _df(close)
    p = StrategyParams()
    out = _regime_signals(df, p)
    mrev = _mean_reversion_signals(df, dataclasses.replace(p, meanrev_trend_filter=False))
    side = out["regime"] == "sideways"
    assert (out.loc[side, "exit"] == mrev["exit"][side]).all()   # sideways unchanged
    bear = out["regime"] == "bear"
    if bear.any():
        assert out.loc[bear, "exit"].all()                        # bear -> cash
