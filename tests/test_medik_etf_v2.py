"""Tests for MEDIK ETF v2 — the cost-aware layer.

The point of v2 is that a technically perfect setup can still be a losing
trade once friction is paid. These tests pin that behaviour down.
"""
from __future__ import annotations

import pytest

from strategy.medik_etf import CandidateScore
from strategy.medik_etf_v2 import (
    MIN_EDGE_MULTIPLE,
    MIN_SCORE_V2,
    MOMENTUM_FAIL_BARS,
    REENTRY_COOLDOWN_SEC_V2,
    V2_UNIVERSE,
    commission,
    minimum_viable_equity,
    momentum_failed,
    net_edge_check,
    qualifies_v2,
    round_trip_cost,
    spread_bps,
)


def _cs(score=90.0, signal="TRADE", reclaim=True, symbol="TQQQ"):
    return CandidateScore(symbol, score, signal, 70.0, 60.0, 2.0, 1.0, 69.0,
                          0.04, True, reclaim, "BULLISH")


# ------------------------------------------------------------------ universe


def test_inverse_funds_are_excluded():
    for sym in ("SQQQ", "SOXS", "TZA", "LABD", "FAZ", "ERY"):
        assert sym not in V2_UNIVERSE


def test_core_momentum_and_broad_funds_are_present():
    for sym in ("SNXX", "TQQQ", "SOXL", "QQQ", "SPY", "IWM"):
        assert sym in V2_UNIVERSE


def test_universe_has_no_duplicates():
    assert len(V2_UNIVERSE) == len(set(V2_UNIVERSE))


# -------------------------------------------------------------- cost model


def test_commission_matches_the_account_schedule():
    assert commission(1, 70) == pytest.approx(0.70)      # 1% cap binds
    assert commission(100, 10_000) == pytest.approx(1.00)  # minimum binds
    assert commission(1000, 100_000) == pytest.approx(5.00)  # per-share binds


def test_leveraged_funds_assumed_to_quote_wider():
    assert spread_bps("TQQQ") > spread_bps("SPY")


def test_round_trip_includes_both_sides_and_friction():
    both = round_trip_cost("TQQQ", 1, 70.0, 70.0)
    one_side = commission(1, 70.0)
    assert both > 2 * one_side          # spread and slippage on top


def test_cost_rises_with_position_size():
    assert round_trip_cost("TQQQ", 10, 70.0, 70.0) > round_trip_cost("TQQQ", 1, 70.0, 70.0)


# --------------------------------------------------------- the net-edge gate


def test_a_tiny_target_fails_the_edge_gate():
    """The v1 failure mode: a valid setup whose target cannot pay its costs."""
    check = net_edge_check("TQQQ", 1, 70.0, 69.5, 70.75)   # 0.75 gross
    assert not check.passes
    assert "NET EDGE FAIL" in check.reason
    assert check.ratio < MIN_EDGE_MULTIPLE


def test_a_large_enough_target_passes():
    check = net_edge_check("TQQQ", 10, 70.0, 68.0, 73.0)   # $30 gross
    assert check.passes
    assert check.ratio >= MIN_EDGE_MULTIPLE


def test_the_gate_is_a_ratio_not_a_sign_test():
    """A profitable-looking trade still fails if the margin is thin."""
    check = net_edge_check("TQQQ", 1, 70.0, 69.0, 71.6)
    assert check.expected_gross > check.cost      # nominally profitable
    assert not check.passes                       # but not by enough


def test_edge_check_reports_its_numbers():
    check = net_edge_check("SPY", 5, 100.0, 98.0, 103.0)
    assert check.expected_gross == pytest.approx(15.0)
    assert check.cost > 0
    assert check.required == MIN_EDGE_MULTIPLE


def test_zero_quantity_cannot_pass():
    assert not net_edge_check("TQQQ", 0, 70.0, 69.0, 72.0).passes


# ------------------------------------------------------ stronger confirmation


def test_v2_requires_a_higher_score_than_v1():
    assert MIN_SCORE_V2 > 75.0
    ok, why = qualifies_v2(_cs(score=80.0))
    assert not ok and "below v2 floor" in why


def test_v2_requires_the_reclaim_not_just_a_breakout():
    ok, why = qualifies_v2(_cs(reclaim=False))
    assert not ok and "reclaim" in why


def test_a_clean_v2_setup_qualifies():
    ok, why = qualifies_v2(_cs())
    assert ok and "reclaim confirmed" in why


def test_non_trade_signals_are_rejected():
    assert not qualifies_v2(_cs(signal="WATCH"))[0]
    assert not qualifies_v2(_cs(signal="REJECT"))[0]


# ------------------------------------------------------------ faster exits


def test_momentum_failure_triggers_after_the_configured_bars():
    assert momentum_failed(MOMENTUM_FAIL_BARS - 1) is False
    assert momentum_failed(MOMENTUM_FAIL_BARS) is True


def test_v2_cooldown_is_longer_than_v1():
    from strategy.medik_etf import REENTRY_COOLDOWN_SEC
    assert REENTRY_COOLDOWN_SEC_V2 > REENTRY_COOLDOWN_SEC


# --------------------------------------------------- minimum viable equity


def test_minimum_viable_equity_is_far_above_the_current_account():
    mve = minimum_viable_equity()
    assert mve > 290.0
    assert mve < 100_000.0        # sanity: it is a real number, not infinity


def test_a_stricter_edge_requirement_raises_the_bar():
    assert minimum_viable_equity(min_multiple=3.0) > minimum_viable_equity(min_multiple=1.5)


def test_tighter_risk_raises_the_bar():
    """Risking less per trade caps the winner, so costs need a bigger account.

    risk_pct is a PERCENT here, matching strategy.medik_etf.
    """
    assert minimum_viable_equity(risk_pct=0.25) > minimum_viable_equity(risk_pct=0.5)


def test_the_current_account_cannot_satisfy_the_edge_gate():
    """The decisive v2 finding, pinned as a regression test.

    At $290 the risk rule caps a 1.5R winner at 0.75% of equity = $2.17,
    against ~$1.44 of round-trip cost. No entry filter can change that.
    """
    equity, price = 290.0, 70.0
    risk_budget = equity * 0.005
    max_gross_win = risk_budget * 1.5
    cost = round_trip_cost("TQQQ", 1, price, price)
    assert max_gross_win / cost < MIN_EDGE_MULTIPLE
