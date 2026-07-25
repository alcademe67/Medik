"""Tests for trailing / breakeven stop logic and high-water persistence."""

import pytest

from bot import risk, state
from bot.config import RiskParams


def test_update_stop_disabled_leaves_stop_but_tracks_high_water():
    p = RiskParams(breakeven_trigger_pct=0.0, trailing_atr_mult=0.0)
    new_stop, hw = risk.update_stop(
        entry_price=100, current_stop=90, high_water=100, price=105, atr=5, params=p
    )
    assert new_stop == 90        # both features off -> stop unchanged
    assert hw == 105             # high-water still tracks the max price


def test_update_stop_breakeven_moves_stop_to_entry():
    p = RiskParams(breakeven_trigger_pct=1.0, trailing_atr_mult=0.0)
    new_stop, _ = risk.update_stop(
        entry_price=100, current_stop=90, high_water=100, price=101, atr=5, params=p
    )
    assert new_stop == pytest.approx(100.0)   # +1% -> protect entry


def test_update_stop_trailing_only_moves_up():
    p = RiskParams(breakeven_trigger_pct=0.0, trailing_atr_mult=2.0)
    # price 120, atr 5 -> trail = 120 - 10 = 110 (above current 90)
    new_stop, hw = risk.update_stop(
        entry_price=100, current_stop=90, high_water=100, price=120, atr=5, params=p
    )
    assert hw == 120 and new_stop == pytest.approx(110.0)
    # pullback to 112: high-water holds at 120, stop must NOT drop back down
    new_stop2, hw2 = risk.update_stop(
        entry_price=100, current_stop=110, high_water=120, price=112, atr=5, params=p
    )
    assert hw2 == 120 and new_stop2 == pytest.approx(110.0)   # never loosens


def test_state_high_water_persists_and_updates(tmp_path):
    db = str(tmp_path / "s.sqlite3")
    state.init_db(db)
    pid = state.add_position(
        state.Position("BTC-USDT", "buy", 1.0, 100, 90, 120, 0.0, "oid", high_water=100), db
    )
    pos = next(p for p in state.open_positions(db) if p.id == pid)
    assert pos.high_water == 100

    state.update_stop(pid, 95, 130, db)
    pos = next(p for p in state.open_positions(db) if p.id == pid)
    assert pos.stop == 95 and pos.high_water == 130
