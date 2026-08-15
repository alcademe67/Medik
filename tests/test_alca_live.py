"""Tests for the hard live-trading limits."""
from __future__ import annotations

import pytest

from strategy.alca_live import (
    DEFAULT_LIMITS,
    LiveLimits,
    SessionState,
    check_new_trade,
    register_close,
    register_fill,
    resolve_allow_short,
)
from strategy.alca_mtf import MIN_VOLUME_RATIO, OHLCV, volume_ratio_of


def _state(**kw) -> SessionState:
    base = dict(equity=1000.0, equity_start_of_day=1000.0, equity_start_of_week=1000.0)
    base.update(kw)
    return SessionState(**base)


# ------------------------------------------------------------- config values


def test_defaults_match_the_strategy_config():
    assert DEFAULT_LIMITS.max_trades_per_day == 4
    assert DEFAULT_LIMITS.max_daily_loss_pct == 1.5
    assert DEFAULT_LIMITS.max_weekly_loss_pct == 3.0
    assert DEFAULT_LIMITS.allow_averaging is False
    assert DEFAULT_LIMITS.max_position_per_symbol == 1


# ------------------------------------------------------------- ALLOW_SHORT


def test_shorting_is_suppressed_when_the_account_cannot_short():
    """ALLOW_SHORT=True in config must not be enough on its own."""
    limits = LiveLimits(allow_short=True)
    effective, why = resolve_allow_short(limits, account_can_short=False)
    assert effective is False
    assert "liquidate" in why


def test_shorting_enabled_only_when_config_and_account_agree():
    assert resolve_allow_short(LiveLimits(allow_short=True), True)[0] is True
    assert resolve_allow_short(LiveLimits(allow_short=False), True)[0] is False
    assert resolve_allow_short(LiveLimits(allow_short=False), False)[0] is False


def test_resolution_always_explains_itself():
    for cfg in (True, False):
        for acct in (True, False):
            _, why = resolve_allow_short(LiveLimits(allow_short=cfg), acct)
            assert why


# ------------------------------------------------------------ trade limits


def test_clean_state_allows_a_trade():
    allowed, reason = check_new_trade("AAPL", _state())
    assert allowed and reason == ""


def test_daily_trade_cap():
    assert check_new_trade("AAPL", _state(trades_today=3))[0] is True
    allowed, reason = check_new_trade("AAPL", _state(trades_today=4))
    assert not allowed and "daily trade cap" in reason


def test_daily_loss_limit_blocks_at_1_5_pct():
    assert check_new_trade("AAPL", _state(equity=986.0))[0] is True      # -1.4%
    allowed, reason = check_new_trade("AAPL", _state(equity=985.0))      # -1.5%
    assert not allowed and "daily loss limit" in reason


def test_weekly_loss_limit_blocks_at_3_pct():
    # inside the day's limit but past the week's
    state = _state(equity=970.0, equity_start_of_day=980.0, equity_start_of_week=1000.0)
    allowed, reason = check_new_trade("AAPL", state)
    assert not allowed and "weekly loss limit" in reason


def test_gains_never_block():
    assert check_new_trade("AAPL", _state(equity=1200.0))[0] is True


def test_one_position_per_symbol():
    state = _state(open_symbols={"AAPL": 1})
    allowed, reason = check_new_trade("AAPL", state)
    assert not allowed and "already holding" in reason
    assert check_new_trade("MSFT", state)[0] is True      # a different symbol is fine


def test_averaging_down_is_refused_even_when_the_symbol_cap_allows_it():
    limits = LiveLimits(max_position_per_symbol=2, allow_averaging=False)
    allowed, reason = check_new_trade("AAPL", _state(open_symbols={"AAPL": 1}), limits)
    assert not allowed and "averaging" in reason


def test_averaging_permitted_when_explicitly_enabled():
    limits = LiveLimits(max_position_per_symbol=2, allow_averaging=True)
    assert check_new_trade("AAPL", _state(open_symbols={"AAPL": 1}), limits)[0] is True


# ------------------------------------------------------------- bookkeeping


def test_fill_and_close_bookkeeping():
    state = _state()
    register_fill(state, "AAPL")
    assert state.trades_today == 1 and state.open_symbols == {"AAPL": 1}
    register_close(state, "AAPL")
    assert state.open_symbols == {}
    # closing does NOT refund the daily trade budget
    assert state.trades_today == 1


def test_closing_a_position_does_not_reopen_the_daily_budget():
    state = _state()
    for sym in ("A", "B", "C", "D"):
        register_fill(state, sym)
        register_close(state, sym)
    assert check_new_trade("E", state)[0] is False


# ------------------------------------------------------------ volume ratio


def test_volume_ratio_excludes_the_current_bar_from_its_own_average():
    bars = [OHLCV(1, 1, 1, 1, 100.0) for _ in range(20)]
    bars.append(OHLCV(1, 1, 1, 1, 300.0))
    assert volume_ratio_of(bars) == pytest.approx(3.0)


def test_volume_ratio_defaults_to_neutral_without_enough_history():
    assert volume_ratio_of([OHLCV(1, 1, 1, 1, 100.0)] * 5) == 1.0
    assert volume_ratio_of([]) == 1.0


def test_volume_ratio_handles_a_zero_baseline():
    bars = [OHLCV(1, 1, 1, 1, 0.0) for _ in range(20)] + [OHLCV(1, 1, 1, 1, 500.0)]
    assert volume_ratio_of(bars) == 1.0


def test_min_volume_ratio_is_a_hard_gate():
    from strategy.alca_mtf import generate_signal

    up = [OHLCV(c, c * 1.005, c * 0.995, c, 1_000_000)
          for c in [100 + i * 1.5 for i in range(80)]]
    weak = generate_signal("TEST", up, up, up, avg_daily_volume=5_000_000,
                           volume_ratio=MIN_VOLUME_RATIO - 0.01)
    assert weak.action == "HOLD"
    assert "volume ratio" in weak.reason
