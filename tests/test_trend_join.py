"""Tests for the Trend Join Long strategy (strategy/trend_join.py).

Pure-logic only — nothing here touches IBKR.
"""
from __future__ import annotations

import json
from datetime import time

import pytest

from strategy.trend_join import (
    Bar,
    EntryRejected,
    LivePosition,
    RulesError,
    Snapshot,
    build_entry,
    evaluate_exits,
    evaluate_filters,
    gap_pct,
    load_rules,
    relative_volume,
    r_multiple,
    swing_low_5m,
    today_high_before_last_bar,
)

RULES = load_rules()


def _bars(lows, highs=None):
    highs = highs or [l + 1 for l in lows]
    return [Bar(open=l, high=h, low=l, close=h, volume=1000) for l, h in zip(lows, highs)]


def _passing_snapshot(**overrides) -> Snapshot:
    """A snapshot engineered to pass every filter, so each test can break one."""
    bars = [Bar(open=100, high=101, low=99, close=100.5, volume=1000) for _ in range(5)]
    bars.append(Bar(open=100.5, high=106, low=100, close=105, volume=5000))
    base = dict(
        symbol="TEST",
        price=105.0,
        prior_day_high=100.0,
        prior_day_close=100.0,
        daily_closes=[50.0] * 199 + [100.0],
        today_open=104.0,          # +4% gap
        premarket_high=104.5,
        bars_5m=bars,
        volume_today=300_000,
        avg_daily_volume=500_000,
        session_fraction_elapsed=0.2,   # RVOL = 300k / 100k = 3.0
    )
    base.update(overrides)
    return Snapshot(**base)


NOON = time(12, 0)


# --------------------------------------------------------------------- config


def test_rules_load_from_repo_file():
    assert RULES.strategy_name == "Trend Join Long"
    assert RULES.direction == "long_only"
    assert RULES.max_concurrent_positions == 5
    assert RULES.partial_profit_trigger_r == 0.75
    assert RULES.earliest_entry_et == time(10, 5)
    assert RULES.force_close_et == time(15, 51)


def test_unknown_top_level_key_is_rejected(tmp_path):
    raw = json.loads((tmp_path / "..").parent.joinpath("rules.json").read_text()) if False else None
    from strategy.trend_join import DEFAULT_RULES_PATH
    raw = json.loads(DEFAULT_RULES_PATH.read_text())
    raw["typo_section"] = {}
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(RulesError, match="unknown top-level"):
        load_rules(p)


def test_missing_section_is_rejected(tmp_path):
    from strategy.trend_join import DEFAULT_RULES_PATH
    raw = json.loads(DEFAULT_RULES_PATH.read_text())
    del raw["risk"]
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(RulesError, match="missing section"):
        load_rules(p)


def test_short_direction_is_rejected(tmp_path):
    """The account is a TFSA and cannot short. Config must not enable it."""
    from strategy.trend_join import DEFAULT_RULES_PATH
    raw = json.loads(DEFAULT_RULES_PATH.read_text())
    raw["direction"] = "long_short"
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(RulesError, match="long-only"):
        load_rules(p)


def test_out_of_order_time_filter_is_rejected(tmp_path):
    from strategy.trend_join import DEFAULT_RULES_PATH
    raw = json.loads(DEFAULT_RULES_PATH.read_text())
    raw["time_filter"]["force_close_et"] = "09:00"
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(RulesError, match="time_filter"):
        load_rules(p)


# -------------------------------------------------------------------- filters


def test_clean_setup_passes_every_filter():
    result = evaluate_filters(_passing_snapshot(), NOON, RULES)
    assert result.passed, result.reasons
    assert all(result.checks.values())


@pytest.mark.parametrize(
    "override, failing_check",
    [
        ({"price": 2.0}, "min_price"),
        ({"prior_day_high": 200.0}, "D1_above_prior_day_high"),
        ({"daily_closes": [200.0] * 200}, "D2_prior_close_above_sma200"),
        ({"today_open": 100.5}, "D3_min_gap_pct"),          # +0.5% gap
        ({"premarket_high": 120.0}, "I1_above_premarket_high"),
        ({"volume_today": 50_000}, "I3_rvol_min"),          # RVOL 0.5
    ],
)
def test_each_filter_can_fail_independently(override, failing_check):
    result = evaluate_filters(_passing_snapshot(**override), NOON, RULES)
    assert not result.passed
    assert result.checks[failing_check] is False
    assert result.reasons


def test_time_window_rejects_before_and_after():
    snap = _passing_snapshot()
    assert not evaluate_filters(snap, time(9, 45), RULES).checks["time_window"]
    assert not evaluate_filters(snap, time(15, 45), RULES).checks["time_window"]
    assert evaluate_filters(snap, time(10, 5), RULES).checks["time_window"]
    assert evaluate_filters(snap, time(15, 30), RULES).checks["time_window"]


def test_sma200_needs_200_closes():
    snap = _passing_snapshot(daily_closes=[50.0] * 199)
    result = evaluate_filters(snap, NOON, RULES)
    assert result.checks["D2_prior_close_above_sma200"] is False
    assert any("need 200" in r for r in result.reasons)


def test_hod_excludes_the_current_bar():
    """I2 must not compare price against a high that includes the live bar."""
    bars = _bars([100, 100, 100], highs=[101, 102, 110])
    assert today_high_before_last_bar(bars) == 102
    assert today_high_before_last_bar(bars[:1]) is None


def test_i2_fails_when_not_breaking_out():
    bars = [Bar(open=100, high=120, low=99, close=100, volume=1000) for _ in range(3)]
    bars.append(Bar(open=100, high=106, low=100, close=105, volume=5000))
    result = evaluate_filters(_passing_snapshot(bars_5m=bars), NOON, RULES)
    assert result.checks["I2_above_today_hod"] is False


def test_gap_and_rvol_math():
    assert gap_pct(103.0, 100.0) == pytest.approx(3.0)
    assert gap_pct(100.0, 0.0) == 0.0
    snap = _passing_snapshot(volume_today=200_000, avg_daily_volume=1_000_000,
                             session_fraction_elapsed=0.1)
    assert relative_volume(snap) == pytest.approx(2.0)
    assert relative_volume(_passing_snapshot(avg_daily_volume=0)) == 0.0


# --------------------------------------------------------------------- sizing


def test_entry_respects_the_tighter_of_the_two_caps():
    snap = _passing_snapshot()
    plan = build_entry(snap, RULES, equity=10_000, open_position_count=0)
    # stop = min low (99) * 0.99 = 98.01, risk/share = 105 - 98.01 = 6.99
    assert plan.stop == pytest.approx(98.01)
    assert plan.risk_per_share == pytest.approx(6.99)
    # risk cap: 1% of 10k = $100 / 6.99 = 14.31 sh; notional cap: 10% = $1000 / 105 = 9.52 sh
    assert plan.quantity == pytest.approx(9.5238, rel=1e-3)
    assert plan.binding_cap == "position_size"
    assert plan.position_value <= 10_000 * 0.10 + 1e-9
    assert plan.risk_dollars <= 10_000 * 0.01 + 1e-9


def test_risk_cap_binds_when_the_stop_is_wide():
    bars = [Bar(open=100, high=101, low=50, close=100, volume=1000) for _ in range(2)]
    bars.append(Bar(open=100, high=106, low=100, close=105, volume=5000))
    snap = _passing_snapshot(bars_5m=bars)
    plan = build_entry(snap, RULES, equity=10_000, open_position_count=0)
    assert plan.binding_cap == "risk_per_trade"
    assert plan.risk_dollars == pytest.approx(100.0)


def test_max_concurrent_positions_blocks_entry():
    with pytest.raises(EntryRejected, match="max_concurrent_positions"):
        build_entry(_passing_snapshot(), RULES, equity=10_000, open_position_count=5)


def test_stop_above_entry_is_rejected():
    bars = [Bar(open=100, high=101, low=200, close=100, volume=1000)]
    snap = _passing_snapshot(bars_5m=bars, price=105.0)
    with pytest.raises(EntryRejected, match="not below entry"):
        build_entry(snap, RULES, equity=10_000, open_position_count=0)


def test_tiny_account_is_rejected_rather_than_rounded_to_zero():
    with pytest.raises(EntryRejected, match="too small"):
        build_entry(_passing_snapshot(), RULES, equity=0.001, open_position_count=0)


# ---------------------------------------------------------------------- exits


def _pos(**kw) -> LivePosition:
    base = dict(symbol="TEST", entry=100.0, stop=98.0, quantity=10.0,
                initial_risk_per_share=2.0)
    base.update(kw)
    return LivePosition(**base)


def test_r_multiple():
    assert r_multiple(_pos(), 102.0) == pytest.approx(1.0)
    assert r_multiple(_pos(), 101.5) == pytest.approx(0.75)
    assert r_multiple(_pos(), 99.0) == pytest.approx(-0.5)


def test_stop_hit_takes_priority_over_everything():
    actions = evaluate_exits(_pos(), _passing_snapshot(price=98.0), NOON, RULES)
    assert [a.kind for a in actions] == ["stop_hit"]
    assert actions[0].quantity == 10.0


def test_force_close_fires_at_the_configured_time():
    snap = _passing_snapshot(price=110.0)
    assert evaluate_exits(_pos(), snap, time(15, 50), RULES)[0].kind != "force_close"
    late = evaluate_exits(_pos(), snap, time(15, 51), RULES)
    assert [a.kind for a in late] == ["force_close"]
    assert late[0].quantity == 10.0


def test_partial_at_075R_then_breakeven_at_1R():
    snap = _passing_snapshot(price=101.5)   # 0.75R
    actions = evaluate_exits(_pos(), snap, NOON, RULES)
    kinds = [a.kind for a in actions]
    assert "partial" in kinds
    partial = next(a for a in actions if a.kind == "partial")
    assert partial.quantity == pytest.approx(10.0 * 0.3333)

    snap = _passing_snapshot(price=102.0)   # 1R
    actions = evaluate_exits(_pos(), snap, NOON, RULES)
    move = next(a for a in actions if a.kind == "move_stop")
    assert move.new_stop == pytest.approx(100.0)


def test_partial_is_not_repeated():
    snap = _passing_snapshot(price=101.5)
    actions = evaluate_exits(_pos(partial_taken=True), snap, NOON, RULES)
    assert not any(a.kind == "partial" for a in actions)


def test_swing_low_needs_confirmation_on_both_sides():
    # index 2 is the pivot: lower than the two before and the two after
    bars = _bars([105, 103, 100, 102, 104])
    assert swing_low_5m(bars) == 100
    # the last bar can never be a confirmed pivot — no bars to its right
    assert swing_low_5m(_bars([105, 104, 103, 102, 99])) is None
    assert swing_low_5m(_bars([100, 101])) is None


def test_trail_only_tightens():
    bars = _bars([105, 103, 100, 102, 104])
    snap = _passing_snapshot(price=110.0, bars_5m=bars)
    # stop already at breakeven (100) and the swing low is also 100 -> no move
    assert not [a for a in evaluate_exits(_pos(stop=100.0), snap, NOON, RULES)
                if a.kind == "move_stop"]
    # stop below the swing low -> trail up to it
    moves = [a for a in evaluate_exits(_pos(stop=99.5, entry=99.0), snap, NOON, RULES)
             if a.kind == "move_stop"]
    assert moves and moves[0].new_stop == 100
