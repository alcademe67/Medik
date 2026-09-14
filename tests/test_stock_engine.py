"""Unit tests for the stock decision engine's pure logic."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.stock_engine import (
    MAX_HOLD_SESSIONS,
    evaluate_stock_entry,
    size_stock,
    stock_exit,
)


# ------------------------------------------------------------------ sizing

def test_size_is_risk_based_and_whole_of_min():
    # equity 500, risk 1% = $5; per-share risk $5 -> 1 share; not capped tighter
    assert size_stock(500, 200, 0, entry=50, stop=45) == 1.0

def test_size_capped_by_available_cash():
    # cash only $30, entry $50 -> can't afford a full share -> 0.6
    assert size_stock(500, 30, 0, entry=50, stop=45) == 0.6

def test_size_capped_by_portfolio_room():
    # already 80% deployed of a 500 equity -> no room
    assert size_stock(500, 500, deployed=400, entry=50, stop=45) == 0.0

def test_size_zero_on_bad_stop():
    assert size_stock(500, 500, 0, entry=50, stop=50) == 0.0
    assert size_stock(500, 500, 0, entry=50, stop=60) == 0.0


# -------------------------------------------------------------------- exits

def test_exit_stop_has_priority_and_gap_fills_at_open():
    # low pierces the stop; a gap-down open below the stop fills at the OPEN
    done, px, why = stock_exit(bar_open=44.0, bar_high=46, bar_low=43.5, bar_close=45,
                               stop=45.0, highest_close=60, atr_now=1.0,
                               closes_below_50=0, sessions_held=3)
    assert done and why == "stop" and px == 44.0

def test_exit_atr_trailing():
    done, px, why = stock_exit(50, 51, 49.5, 49.6, stop=40, highest_close=60,
                               atr_now=3.0, closes_below_50=0, sessions_held=5)
    # trail = 60 - 3*3 = 51; close 49.6 <= 51 -> exit
    assert done and why == "atr_trail"

def test_exit_trend_break_after_two_closes():
    done, _, why = stock_exit(50, 51, 49, 49.2, stop=40, highest_close=51,
                              atr_now=1.0, closes_below_50=2, sessions_held=5)
    assert done and why == "trend_break"

def test_exit_max_hold():
    done, _, why = stock_exit(50, 51, 50, 50.5, stop=40, highest_close=51,
                              atr_now=1.0, closes_below_50=0, sessions_held=MAX_HOLD_SESSIONS)
    assert done and why == "max_hold"

def test_hold_when_nothing_triggers():
    done, _, why = stock_exit(50, 51, 49.9, 50.5, stop=40, highest_close=51,
                              atr_now=1.0, closes_below_50=1, sessions_held=5)
    assert not done and why == "hold"


# ------------------------------------------------------------------- entries

def _series(prices, vols=None):
    n = len(prices)
    df = pd.DataFrame({
        "open": prices, "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices], "close": prices,
        "volume": vols if vols is not None else [2_000_000] * n,
    })
    return df

def test_downtrend_is_rejected():
    prices = list(np.linspace(200, 100, 260))     # steady decline
    sig = evaluate_stock_entry(_series(prices))
    assert not sig.passed

def test_insufficient_history_rejected():
    sig = evaluate_stock_entry(_series(list(np.linspace(100, 120, 50))))
    assert not sig.passed and "history" in sig.reason

def test_engine_returns_structurally_valid_signals():
    # Structural test: over a sliding window the engine never errors, every
    # result is a StockSignal, rejections carry a reason, and any PASS has a
    # valid stop strictly below entry. (Actual firing on genuine trends is
    # validated by the real-data backtest, not by hand-fitted synthetic bars.)
    t = np.arange(340)
    base = 80 + t * 0.15 + 6 * np.sin(t / 8.0) + 3 * np.sin(t / 3.0)
    df = _series(list(base), [2_000_000] * len(base))
    for cut in range(len(df) - 60, len(df)):
        s = evaluate_stock_entry(df.iloc[: cut + 1])
        assert isinstance(s.passed, bool)
        assert s.passed or s.reason, "a rejection must explain itself"
        if s.passed:
            assert 0 < s.stop < s.entry
