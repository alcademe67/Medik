"""Tests for the kill switch and startup reconciliation."""
from __future__ import annotations

import pytest

from strategy.medik_etf import Position
from strategy.medik_etf_ops import (
    KILL_SWITCH_PATH,
    WorkingOrder,
    adopt_open_position,
    kill_switch_active,
    kill_switch_reason,
    reconcile_startup,
)

UNIVERSE = ["TQQQ", "SOXL", "QQQ", "SPY"]


def _stop(symbol="TQQQ", qty=5, price=68.0):
    return WorkingOrder(symbol, "SELL", "STP", qty, price)


def _target(symbol="TQQQ", qty=5, price=75.0):
    return WorkingOrder(symbol, "SELL", "LMT", qty, price)


def _pos(symbol="TQQQ", qty=5, value=350.0):
    return Position(symbol, qty, value)


# ------------------------------------------------------------- kill switch


def test_kill_switch_inactive_when_absent(tmp_path):
    assert kill_switch_active(tmp_path / "STOP_MEDIK") is False


def test_kill_switch_active_when_present(tmp_path):
    path = tmp_path / "STOP_MEDIK"
    path.write_text("")
    assert kill_switch_active(path) is True


def test_an_empty_file_still_stops_the_bot(tmp_path):
    """Presence is the signal; content is only for the log."""
    path = tmp_path / "STOP_MEDIK"
    path.write_text("")
    assert kill_switch_active(path) is True
    assert kill_switch_reason(path) == ""


def test_kill_switch_reason_is_read_when_given(tmp_path):
    path = tmp_path / "STOP_MEDIK"
    path.write_text("  stopping for the day  ")
    assert kill_switch_reason(path) == "stopping for the day"


def test_reason_is_truncated_and_never_raises(tmp_path):
    path = tmp_path / "STOP_MEDIK"
    path.write_text("x" * 5000)
    assert len(kill_switch_reason(path)) <= 200
    assert kill_switch_reason(tmp_path / "missing") == ""


def test_default_path_is_the_repo_root():
    assert KILL_SWITCH_PATH.name == "STOP_MEDIK"
    assert (KILL_SWITCH_PATH.parent / "strategy").is_dir()


# --------------------------------------------------------------- adoption


def test_a_position_with_both_legs_is_adopted():
    r = adopt_open_position(_pos(), [_stop(), _target()])
    assert r.protected and r.adopted is not None
    assert r.adopted.symbol == "TQQQ"
    assert r.adopted.quantity == 5
    assert r.adopted.stop == 68.0
    assert r.adopted.target == 75.0
    assert r.adopted.entry == pytest.approx(70.0)      # 350 / 5


def test_a_position_with_no_orders_is_unprotected():
    r = adopt_open_position(_pos(), [])
    assert not r.protected and r.adopted is None
    assert "UNPROTECTED" in r.reason


def test_a_missing_stop_is_unprotected():
    r = adopt_open_position(_pos(), [_target()])
    assert not r.protected
    assert "NO STOP" in r.reason


def test_a_missing_target_is_reported_but_not_adopted():
    r = adopt_open_position(_pos(), [_stop()])
    assert not r.protected
    assert "cannot manage the exit" in r.reason


def test_partial_coverage_is_refused():
    """Legs covering fewer shares than are held is not a bracket."""
    r = adopt_open_position(_pos(qty=10), [_stop(qty=4), _target(qty=4)])
    assert not r.protected
    assert "PARTIALLY UNPROTECTED" in r.reason


def test_orders_for_other_symbols_are_ignored():
    r = adopt_open_position(_pos(), [_stop("SOXL"), _target("SOXL")])
    assert not r.protected


def test_buy_orders_are_not_mistaken_for_protection():
    legs = [WorkingOrder("TQQQ", "BUY", "STP", 5, 68.0),
            WorkingOrder("TQQQ", "BUY", "LMT", 5, 75.0)]
    assert not adopt_open_position(_pos(), legs).protected


def test_the_tightest_stop_and_nearest_target_win():
    legs = [_stop(price=66.0), _stop(price=69.0), _target(price=78.0), _target(price=74.0)]
    r = adopt_open_position(_pos(), legs)
    assert r.adopted.stop == 69.0
    assert r.adopted.target == 74.0


def test_zero_quantity_is_not_a_position():
    assert adopt_open_position(_pos(qty=0), []).reason == "no position"


# ----------------------------------------------------- the decision table


def test_flat_starts_normally():
    d = reconcile_startup([], [], UNIVERSE)
    assert d.action == "START" and d.may_trade and d.adopted is None


def test_position_with_valid_bracket_is_adopted():
    d = reconcile_startup([_pos()], [_stop(), _target()], UNIVERSE)
    assert d.action == "ADOPT" and d.may_trade
    assert d.adopted.symbol == "TQQQ"


def test_position_with_missing_bracket_refuses():
    d = reconcile_startup([_pos()], [], UNIVERSE)
    assert d.action == "REFUSE" and not d.may_trade
    assert d.adopted is None


def test_unexpected_position_refuses():
    """A holding outside the universe means the bot's model is already wrong."""
    d = reconcile_startup([_pos("AAPL", 10, 2000.0)], [], UNIVERSE)
    assert d.action == "REFUSE"
    assert any("UNEXPECTED" in n for n in d.notes)


def test_an_ignored_symbol_does_not_block_startup():
    d = reconcile_startup([_pos("QQQ", 0.2836, 203.11)], [], UNIVERSE,
                          ignore_symbols=("QQQ",))
    assert d.action == "START"
    assert any("explicitly ignored" in n for n in d.notes)


def test_the_core_holding_blocks_by_default():
    """QQQ is in the universe with no bracket, so the default is REFUSE.

    This is the live account's actual state, and it is the safe answer: the
    strategy must not silently adopt a long-term holding as an intraday trade.
    """
    d = reconcile_startup([_pos("QQQ", 0.2836, 203.11)], [], UNIVERSE)
    assert d.action == "REFUSE"


def test_two_adoptable_positions_refuse():
    positions = [_pos("TQQQ"), _pos("SOXL", 3, 90.0)]
    orders = [_stop(), _target(), _stop("SOXL", 3, 28.0), _target("SOXL", 3, 34.0)]
    d = reconcile_startup(positions, orders, UNIVERSE)
    assert d.action == "REFUSE"
    assert any("one-position rule" in n for n in d.notes)


def test_zero_quantity_positions_are_skipped():
    d = reconcile_startup([Position("TQQQ", 0, 0.0)], [], UNIVERSE)
    assert d.action == "START"
