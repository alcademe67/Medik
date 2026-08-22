"""Tests for the promotion gates.

The gates decide whether a strategy moves toward real money, so they need to
be as hard to pass by accident as the strategy's own authorisation checks.
"""
from __future__ import annotations

import pytest

from backtest.medik_etf_bt import (
    MAX_DRAWDOWN_PCT,
    MIN_EXPECTANCY_COST_MULTIPLE,
    MIN_OOS_PROFIT_FACTOR,
    MIN_OOS_TRADES,
    SymbolResult,
    evaluate_gates,
)

EQUITY = 1000.0


def _trade(net, commission=0.50, i=0):
    """One trade. `i` must produce MONOTONIC timestamps: the drawdown is
    computed in chronological order, so a helper that cycles dates would
    silently reorder the equity curve and hide the very thing being tested.
    """
    stamp = f"2026-03-01T{9 + i // 60:02d}:{i % 60:02d}:00"
    return {"symbol": "TQQQ", "entry_time": stamp, "exit_time": stamp,
            "entry": 70.0, "exit": 70.0 + net, "qty": 1, "reason": "target",
            "gross": net + commission, "commission": commission, "net": net,
            "bars_held": 6, "notional": 70.0}


def _result(nets, commission=0.50):
    r = SymbolResult("TQQQ")
    r.trades = [_trade(n, commission, i) for i, n in enumerate(nets)]
    r.sessions = max(1, len(nets))
    return [r]


def _passing_nets(n=40):
    """Comfortably profitable: 60% winners at +$4, 40% losers at -$2."""
    return [4.0 if i % 10 < 6 else -2.0 for i in range(n)]


# --------------------------------------------------------- a passing result


def test_a_strong_result_promotes():
    v = evaluate_gates(_result(_passing_nets()), EQUITY)
    assert v["promote"] is True
    assert all(g[3] for g in v["gates"])


def test_every_gate_is_reported_even_when_passing():
    v = evaluate_gates(_result(_passing_nets()), EQUITY)
    names = [g[0] for g in v["gates"]]
    for expected in ("net profit factor", "out-of-sample trades",
                     "expectancy vs cost", "max drawdown", "net P&L positive"):
        assert expected in names


# ------------------------------------------------------- each gate can fail


def test_too_few_trades_blocks_promotion():
    """The gate most likely to fail, and the one that matters most."""
    v = evaluate_gates(_result(_passing_nets(MIN_OOS_TRADES - 1)), EQUITY)
    assert not v["promote"]
    assert not dict((g[0], g[3]) for g in v["gates"])["out-of-sample trades"]


def test_a_marginal_profit_factor_blocks_promotion():
    """PF just above 1.0 is noise. The gate rejects what the older
    strategies in this repo would have passed."""
    nets = [1.0 if i % 2 else -0.95 for i in range(40)]      # PF ~1.05
    v = evaluate_gates(_result(nets), EQUITY)
    assert 1.0 < v["profit_factor"] < MIN_OOS_PROFIT_FACTOR
    assert not v["promote"]


def test_thin_expectancy_against_costs_blocks_promotion():
    """Profitable but not worth doing: pennies against a real round trip."""
    nets = [0.30 if i % 10 < 7 else -0.20 for i in range(40)]
    v = evaluate_gates(_result(nets, commission=1.50), EQUITY)
    assert v["net"] > 0
    assert not dict((g[0], g[3]) for g in v["gates"])["expectancy vs cost"]
    assert not v["promote"]


def test_a_deep_drawdown_blocks_promotion():
    """Ends profitable, but via a hole the operator would not sit through."""
    nets = [-8.0] * 25 + [20.0] * 25
    v = evaluate_gates(_result(nets), EQUITY)
    assert v["net"] > 0
    assert v["max_drawdown"] > MAX_DRAWDOWN_PCT
    assert not v["promote"]


def test_a_losing_strategy_blocks_promotion():
    v = evaluate_gates(_result([-1.0] * 40), EQUITY)
    assert v["net"] < 0
    assert not v["promote"]


# ------------------------------------------------------------ edge cases


def test_no_trades_cannot_promote():
    """An empty result is not a pass — nothing was demonstrated."""
    v = evaluate_gates([SymbolResult("TQQQ")], EQUITY)
    assert not v["promote"]
    assert v["trades"] == 0


def test_all_winners_does_not_bypass_the_trade_count():
    """Infinite profit factor on 5 trades is still 5 trades."""
    v = evaluate_gates(_result([5.0] * 5), EQUITY)
    assert v["profit_factor"] == float("inf")
    assert not v["promote"]


def test_gates_combine_across_symbols():
    a, b = _result(_passing_nets(20)), _result(_passing_nets(20))
    v = evaluate_gates(a + b, EQUITY)
    assert v["trades"] == 40
    assert v["promote"] is True


def test_thresholds_are_stricter_than_break_even():
    """Guards the reasoning: 1.0 would be passed by noise."""
    assert MIN_OOS_PROFIT_FACTOR >= 1.3
    assert MIN_OOS_TRADES >= 30
    assert MIN_EXPECTANCY_COST_MULTIPLE >= 2.0
    assert MAX_DRAWDOWN_PCT <= 15.0


def test_drawdown_is_measured_in_trade_order_not_symbol_order():
    """Interleaved symbols must produce a chronological equity curve."""
    r1, r2 = SymbolResult("A"), SymbolResult("B")
    r1.trades = [_trade(-50.0, i=0), _trade(60.0, i=4)]
    r2.trades = [_trade(-50.0, i=1), _trade(60.0, i=5)]
    v = evaluate_gates([r1, r2], EQUITY)
    assert v["max_drawdown"] >= 10.0     # both losses land before both wins
