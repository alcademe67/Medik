"""Every signal must be accounted for: taken, or rejected by a named gate.

skips_other used to be incremented BEFORE signals, so the two counted
different populations and a symbol could report more skips than signals. That
reads as a broken strategy rather than a broken tally -- and when the answer
is "zero trades", the breakdown of WHY is the entire result.
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta

import pytest

from backtest.medik_etf_bt import SymbolResult, backtest_symbol, load_symbol, report


def _bars(symbol_seed: int, price: float, sessions: int = 12):
    """Deterministic 5m bars: a plausible shape, not a market."""
    from strategy.medik_mtf import OHLCV
    rnd = random.Random(symbol_seed)
    bars, times = [], []
    day = datetime(2026, 6, 1, 9, 30)
    while len([t for t in times]) < sessions * 78:
        if day.weekday() < 5:
            t = day.replace(hour=9, minute=30)
            drift = rnd.uniform(-0.0008, 0.0010)
            for _ in range(78):
                step = price * (drift + rnd.gauss(0, 0.0015))
                op, cl = price, max(0.5, price + step)
                bars.append(OHLCV(op, max(op, cl) * 1.0005, min(op, cl) * 0.9995,
                                  cl, float(rnd.randint(80_000, 500_000))))
                times.append(t.strftime("%Y-%m-%dT%H:%M:%S"))
                price = cl
                t += timedelta(minutes=5)
        day += timedelta(days=1)
    return bars, times


def _accounted(r: SymbolResult) -> int:
    return (r.skips_whole_share + r.skips_v2 + r.skips_edge + r.skips_other
            + len(r.trades))


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("equity,price", [(290.0, 68.0), (290.0, 620.0),
                                          (5000.0, 68.0), (25000.0, 250.0)])
def test_signals_equal_skips_plus_trades(version, equity, price):
    bars, times = _bars(7, price)
    r = backtest_symbol("TEST", bars, times, equity, version=version)
    assert _accounted(r) == r.signals, (
        f"{r.signals} signals but {_accounted(r)} accounted for "
        f"(ws={r.skips_whole_share} v2={r.skips_v2} edge={r.skips_edge} "
        f"other={r.skips_other} taken={len(r.trades)})")


def test_no_skip_bucket_can_exceed_signals():
    bars, times = _bars(11, 700.0)
    r = backtest_symbol("TEST", bars, times, 290.0, version="v2")
    for name in ("skips_whole_share", "skips_v2", "skips_edge", "skips_other"):
        assert getattr(r, name) <= r.signals, name


def test_v1_never_uses_the_v2_only_buckets():
    """v1 has no extra setup filter and no net-edge gate."""
    bars, times = _bars(3, 68.0)
    r = backtest_symbol("TEST", bars, times, 5000.0, version="v1")
    assert r.skips_v2 == 0 and r.skips_edge == 0


def test_a_tiny_account_rejects_expensive_etfs_on_whole_shares():
    """$290 cannot buy one share of a $620 ETF inside the risk budget."""
    bars, times = _bars(5, 620.0)
    r = backtest_symbol("TEST", bars, times, 290.0, version="v2")
    assert r.signals > 0, "fixture produced no signals; test proves nothing"
    assert r.skips_whole_share > 0
    assert len(r.trades) == 0


def test_the_report_prints_the_breakdown_even_with_no_trades(capsys):
    bars, times = _bars(5, 620.0)
    r = backtest_symbol("TEST", bars, times, 290.0, version="v2")
    out = report([r], 290.0, "TEST")
    printed = capsys.readouterr().out
    assert "NO TRADES TAKEN" in printed
    assert "rejected, below 1 whole share" in printed
    assert "WARNING" not in printed, "accounting did not reconcile"
    assert out["whole_share_skips"] == r.skips_whole_share


def test_the_report_reconciles_when_trades_are_taken(capsys):
    bars, times = _bars(5, 68.0)
    r = backtest_symbol("TEST", bars, times, 25_000.0, version="v2")
    report([r], 25_000.0, "TEST")
    assert "WARNING" not in capsys.readouterr().out
