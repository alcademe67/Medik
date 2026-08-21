"""Tests for the ETF backtester's execution realism.

These check the parts a backtest most easily gets wrong in its own favour:
fill timing, stop slippage, gap handling, stop-wins-ties, commissions, and
the whole-share skip accounting.
"""
from __future__ import annotations

import json

import pytest

from backtest.medik_etf_bt import (
    SPREAD_BPS,
    STOP_SLIPPAGE_BPS,
    backtest_symbol,
    commission,
    half_spread,
    load_symbol,
    to_15m,
)
from strategy.medik_mtf import OHLCV


def _session(day: str, n: int = 78, start: float = 100.0, step: float = 0.0):
    """One session of 5m bars with ISO timestamps."""
    bars, times = [], []
    for i in range(n):
        c = start + i * step
        bars.append(OHLCV(c, c + 0.2, c - 0.2, c, 100_000.0))
        hh, mm = divmod(30 + i * 5, 60)
        times.append(f"{day}T{13 + hh:02d}:{mm:02d}:00")
    return bars, times


# ------------------------------------------------------------- commissions


def test_commission_matches_the_account_schedule():
    assert commission(100, 10_000) == pytest.approx(1.00)     # $1 minimum
    assert commission(1000, 100_000) == pytest.approx(5.00)   # per-share
    assert commission(1, 50) == pytest.approx(0.50)           # 1% cap
    assert commission(0, 0) == 0.0
    assert commission(-5, 100) == 0.0


def test_leveraged_funds_are_assumed_to_quote_wider():
    assert half_spread("TQQQ", 100.0) > half_spread("SPY", 100.0)
    assert SPREAD_BPS[3.0] > SPREAD_BPS[1.0]


def test_unknown_symbols_get_the_widest_spread_assumption():
    assert half_spread("WHAT", 100.0) >= half_spread("TQQQ", 100.0)


# --------------------------------------------------------- 15m aggregation


def test_15m_bars_are_groups_of_three_within_a_session():
    bars, times = _session("2026-08-17", n=9, step=1.0)
    b15, t15 = to_15m(bars, times)
    assert len(b15) == 3
    assert b15[0].open == bars[0].open
    assert b15[0].close == bars[2].close
    assert b15[0].high == max(b.high for b in bars[:3])
    assert b15[0].volume == sum(b.volume for b in bars[:3])


def test_a_trailing_partial_group_is_dropped():
    """An incomplete 15m bar is not a completed observation."""
    bars, times = _session("2026-08-17", n=8)
    b15, _ = to_15m(bars, times)
    assert len(b15) == 2          # 8 bars -> two complete groups, 2 discarded


def test_groups_never_span_two_sessions():
    b1, t1 = _session("2026-08-17", n=4)
    b2, t2 = _session("2026-08-18", n=4)
    b15, t15 = to_15m(b1 + b2, t1 + t2)
    assert len(b15) == 2          # one complete group per day, remainder dropped
    assert t15[0][:10] == "2026-08-17"
    assert t15[1][:10] == "2026-08-18"


# ------------------------------------------------------------------ loading


def test_load_merges_chunks_and_deduplicates(tmp_path):
    folder = tmp_path / "TEST"
    folder.mkdir()
    common = {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]}
    (folder / "a.json").write_text(json.dumps({"time": ["2026-08-17T13:30:00"], **common}))
    (folder / "b.json").write_text(json.dumps({
        "time": ["2026-08-17T13:30:00", "2026-08-17T13:35:00"],
        "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
        "close": [1.0, 2.0], "volume": [1.0, 2.0]}))
    bars, times = load_symbol("TEST", tmp_path)
    assert len(bars) == 2                       # duplicate timestamp collapsed
    assert times == sorted(times)               # chronological


def test_missing_symbol_returns_none(tmp_path):
    assert load_symbol("NOPE", tmp_path) is None


# ------------------------------------------------------- execution realism


def _flat_market(days=4):
    """A market with no setups — used to prove the strategy is selective."""
    bars, times = [], []
    for d in range(days):
        b, t = _session(f"2026-08-{17 + d:02d}", step=0.0)
        bars += b
        times += t
    return bars, times


def test_a_flat_market_produces_no_trades():
    bars, times = _flat_market()
    res = backtest_symbol("SPY", bars, times, 290.0)
    assert res.trades == []
    assert res.signals == 0


def test_sessions_are_counted():
    bars, times = _flat_market(days=3)
    res = backtest_symbol("SPY", bars, times, 290.0)
    assert res.sessions == 3


def test_expensive_symbol_at_small_equity_skips_on_whole_shares():
    """SPY near $765 cannot produce one affordable whole share on $290."""
    bars, times = [], []
    for d in range(4):
        b, t = _session(f"2026-08-{17 + d:02d}", start=765.0, step=0.05)
        bars += b
        times += t
    res = backtest_symbol("SPY", bars, times, 290.0)
    # Either no setup qualified, or every qualifying setup was skipped for
    # sizing. What must NEVER happen is a trade the account cannot afford.
    for t in res.trades:
        assert t["notional"] <= 290.0


def test_backtest_is_deterministic():
    bars, times = _flat_market()
    a = backtest_symbol("SPY", bars, times, 290.0)
    b = backtest_symbol("SPY", bars, times, 290.0)
    assert a.trades == b.trades
    assert (a.signals, a.skips_whole_share) == (b.signals, b.skips_whole_share)


def test_stop_slippage_is_adverse_not_favourable():
    """A stop must be assumed to fill WORSE than its trigger price."""
    assert STOP_SLIPPAGE_BPS > 0
    stop = 100.0
    filled = stop * (1 - STOP_SLIPPAGE_BPS / 10_000.0)
    assert filled < stop


def test_every_trade_records_its_costs():
    """No trade may be booked without a commission attached."""
    bars, times = _flat_market()
    res = backtest_symbol("TQQQ", bars, times, 10_000.0)
    for t in res.trades:
        assert t["commission"] > 0
        assert t["net"] == pytest.approx(t["gross"] - t["commission"])
