"""Tests for automated live execution of the MEDIK ETF strategy.

Covers the deterministic authorisation checklist that REPLACES per-order
human confirmation, the anti-repeat ledger that stops a 5-minute scan loop
re-submitting the same setup, capital-utilisation vs risk-ceiling
precedence, and the automatic exit/rotation rules.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from strategy.medik_etf import (
    MAX_CAPITAL_UTILIZATION,
    MAX_DAILY_LOSS_PCT,
    MAX_TRADES_PER_SESSION,
    REENTRY_COOLDOWN_SEC,
    ROTATION_MIN_SCORE_MARGIN,
    RISK_PCT_MAX,
    CandidateScore,
    OpenTrade,
    PortfolioState,
    Position,
    SessionControls,
    SizingRejected,
    TradeLedger,
    authorize_order,
    should_exit,
    should_rotate,
    size_trade,
)

NOON = 12 * 60
TS = 1_000_000.0


def _cs(symbol="TQQQ", score=90.0, signal="TRADE", price=50.0, atr=1.0,
        trend="BULLISH", rvol=2.0):
    return CandidateScore(symbol, score, signal, price, 60.0, rvol, atr,
                          price - 1.0, 0.02, True, True, trend)


def _state(equity=10_000.0, cash=10_000.0, positions=(), open_orders=0):
    return PortfolioState(equity, cash, positions, open_orders)


def _controls(**kw):
    base = dict(equity_start_of_session=10_000.0)
    base.update(kw)
    return SessionControls(**base)


def _authorize(**over):
    """Authorise a known-good order, with individual inputs overridable."""
    state = over.pop("state", _state())
    candidate = over.pop("candidate", _cs())
    sized = over.pop("sized", size_trade(candidate, state))
    kwargs = dict(
        live_enabled=True, connected=True, state=state,
        controls=over.pop("controls", _controls()), candidate=candidate,
        sized=sized, now_minutes=NOON,
        ledger=over.pop("ledger", TradeLedger()), now_ts=TS,
    )
    kwargs.update(over)
    return authorize_order(**kwargs)


# ------------------------------------------------- no interactive approval


def test_runner_contains_no_interactive_confirmation():
    """The spec forbids input()/typed approval before an automated trade.

    Checks executable calls, not prose: the module docstring legitimately
    discusses the confirmation it replaced and why.
    """
    lines = [ln for ln in Path("examples/medik_etf_live.py").read_text().splitlines()
             if not ln.lstrip().startswith("#")]
    source = "\n".join(lines)
    for forbidden in ("input(", "raw_input(", "getpass"):
        assert forbidden not in source, f"interactive call {forbidden} present"


def test_authorization_is_pure_and_repeatable():
    """Same inputs must always give the same answer — no model in the loop."""
    first = _authorize()
    second = _authorize()
    assert first.authorized == second.authorized
    assert first.checks == second.checks


# -------------------------------------------------- automatic authorization


def test_a_clean_setup_authorizes_automatically():
    auth = _authorize()
    assert auth.authorized, auth.failures
    assert auth.failures == ()
    assert bool(auth) is True


def test_live_flag_off_blocks_authorization():
    auth = _authorize(live_enabled=False)
    assert not auth
    assert "live_mode_enabled" in auth.failures


def test_disconnected_blocks_authorization():
    auth = _authorize(connected=False)
    assert "ibkr_connected" in auth.failures


def test_invalid_account_data_blocks():
    state = _state(equity=0.0)
    with pytest.raises(SizingRejected):
        size_trade(_cs(), state)


def test_non_tradeable_setup_blocks():
    state = _state()
    cand = _cs(signal="WATCH")
    auth = _authorize(state=state, candidate=cand, sized=size_trade(cand, state))
    assert "setup_valid" in auth.failures


def test_conflicting_position_blocks():
    auth = _authorize(state=_state(positions=(Position("SOXL", 3, 300.0),)))
    assert "no_conflicting_position" in auth.failures


def test_conflicting_open_order_blocks():
    auth = _authorize(state=_state(open_orders=1))
    assert "no_conflicting_open_order" in auth.failures


def test_daily_loss_shutdown_blocks_authorization():
    state = _state(equity=10_000.0 * (1 - MAX_DAILY_LOSS_PCT / 100))
    auth = _authorize(state=state, controls=_controls())
    assert "session_gates_ok" in auth.failures


def test_trade_count_limit_blocks_authorization():
    auth = _authorize(controls=_controls(trades_completed=MAX_TRADES_PER_SESSION))
    assert "session_gates_ok" in auth.failures


def test_disabled_entries_block_authorization():
    controls = _controls()
    controls.disable("bracket failure: stop leg is Cancelled")
    auth = _authorize(controls=controls)
    assert "entries_enabled" in auth.failures


@pytest.mark.parametrize("minutes, label", [(9 * 60 + 40, "opening delay"),
                                            (15 * 60 + 40, "close buffer")])
def test_market_hours_block_authorization(minutes, label):
    auth = _authorize(now_minutes=minutes)
    assert "session_gates_ok" in auth.failures, label


def test_every_documented_check_is_present():
    checks = _authorize().checks
    for name in (
        "live_mode_enabled", "ibkr_connected", "account_data_valid",
        "buying_power_valid", "market_data_valid", "setup_valid",
        "position_size_valid", "whole_share_quantity", "stop_valid",
        "target_valid", "risk_within_limit", "capital_utilization_ok",
        "no_conflicting_position", "no_conflicting_open_order",
        "session_gates_ok", "not_duplicate",
    ):
        assert name in checks, f"missing gate: {name}"


# ------------------------------------------------- duplicate-order prevention


def test_ledger_blocks_a_symbol_while_an_order_is_in_flight():
    ledger = TradeLedger()
    ledger.mark_pending("TQQQ")
    assert ledger.is_blocked("TQQQ", TS) is True
    assert ledger.is_blocked("SOXL", TS) is False


def test_ledger_blocks_re_entry_during_the_cooldown():
    ledger = TradeLedger()
    ledger.mark_entered("TQQQ", TS)
    assert ledger.is_blocked("TQQQ", TS + 60) is True
    assert ledger.is_blocked("TQQQ", TS + REENTRY_COOLDOWN_SEC + 1) is False


def test_a_failed_order_releases_the_symbol():
    """An order that never became a position must not hold the slot."""
    ledger = TradeLedger()
    ledger.mark_pending("TQQQ")
    ledger.mark_failed("TQQQ")
    assert ledger.is_blocked("TQQQ", TS) is False


def test_duplicate_blocks_authorization():
    ledger = TradeLedger()
    ledger.mark_entered("TQQQ", TS)
    auth = _authorize(ledger=ledger, now_ts=TS + 30)
    assert not auth
    assert "not_duplicate" in auth.failures


def test_repeated_scans_within_the_cooldown_produce_one_order():
    """The 5-minute loop seeing an identical qualifying chart must not
    re-submit it. Three ticks fit inside the 15-minute cooldown."""
    ledger, submitted = TradeLedger(), 0
    for tick in range(3):                       # 0, 300, 600 seconds
        now = TS + tick * 300
        if _authorize(ledger=ledger, now_ts=now):
            ledger.mark_entered("TQQQ", now)
            submitted += 1
    assert submitted == 1


def test_re_entry_is_permitted_once_the_cooldown_expires():
    """The ledger is a cooldown, not a permanent ban — a setup that closed
    and re-formed later is a legitimate new trade. In the live loop the
    one-position rule is what prevents doubling up meanwhile."""
    ledger = TradeLedger()
    ledger.mark_entered("TQQQ", TS)
    assert not _authorize(ledger=ledger, now_ts=TS + REENTRY_COOLDOWN_SEC - 1)
    assert _authorize(ledger=ledger, now_ts=TS + REENTRY_COOLDOWN_SEC + 1)


def test_an_open_position_blocks_re_entry_even_after_the_cooldown():
    """The real loop's guard: cooldown expiry cannot double a live position."""
    ledger = TradeLedger()
    ledger.mark_entered("TQQQ", TS)
    held = _state(positions=(Position("TQQQ", 10, 500.0),))
    auth = _authorize(state=held, ledger=ledger, now_ts=TS + REENTRY_COOLDOWN_SEC + 1)
    assert not auth
    assert "no_conflicting_position" in auth.failures


# ------------------------------------------- capital utilisation vs risk


def test_capital_allocation_respects_the_90_percent_ceiling():
    state = _state(equity=10_000.0, cash=1_000.0)
    sized = size_trade(_cs(price=50.0, atr=0.05), state)
    assert sized.notional <= 1_000.0 * MAX_CAPITAL_UTILIZATION + 1e-6
    assert sized.binding_cap == "available_cash"


def test_ninety_percent_is_allocation_not_risk():
    """Allocating 90% of capital must not put 90% of it at risk."""
    state = _state(equity=10_000.0, cash=10_000.0)
    sized = size_trade(_cs(price=50.0, atr=0.05), state)
    assert sized.risk_dollars < sized.notional * 0.10


def test_risk_ceiling_overrides_capital_allocation():
    """A wide stop must shrink the position even with capital available."""
    state = _state(equity=10_000.0, cash=10_000.0)
    tight = size_trade(_cs(price=50.0, atr=0.05), state)
    wide = size_trade(_cs(price=50.0, atr=8.0), state)
    assert wide.quantity < tight.quantity
    assert wide.binding_cap == "risk_per_trade"
    assert wide.risk_dollars <= 10_000.0 * RISK_PCT_MAX / 100.0


def test_never_exceeds_buying_power():
    state = _state(equity=100_000.0, cash=300.0)
    sized = size_trade(_cs(price=50.0, atr=0.5), state)
    assert sized.notional <= 300.0


def test_no_trade_is_forced_just_to_reach_90_percent():
    """Under-utilising capital is fine; breaking the whole-share rule is not."""
    state = _state(equity=290.0, cash=87.0)
    with pytest.raises(SizingRejected, match="below 1 whole share"):
        size_trade(_cs(price=500.0, atr=10.0), state)


def test_authorization_rejects_an_oversized_allocation():
    state = _state(equity=10_000.0, cash=100.0)
    good = size_trade(_cs(price=10.0, atr=0.2), state)
    oversized = type(good)(**{**good.__dict__, "quantity": 50,
                              "notional": 500.0})
    auth = _authorize(state=state, sized=oversized)
    assert "capital_utilization_ok" in auth.failures


# ----------------------------------------------------------- automatic exits


def _trade(entry=50.0, stop=48.0, target=53.0):
    return OpenTrade("TQQQ", 10, entry, stop, target, TS)


def test_exit_on_stop():
    out, why = should_exit(_trade(), _cs(), 47.5, NOON)
    assert out and "stop hit" in why


def test_exit_on_target():
    out, why = should_exit(_trade(), _cs(), 53.5, NOON)
    assert out and "target hit" in why


def test_exit_when_the_regime_breaks_down():
    out, why = should_exit(_trade(), _cs(trend="BEARISH"), 50.5, NOON)
    assert out and "regime" in why


def test_exit_near_the_close():
    out, why = should_exit(_trade(), _cs(), 50.5, 16 * 60 - 4)
    assert out and "session ending" in why


def test_no_exit_while_the_trade_is_working():
    out, why = should_exit(_trade(), _cs(), 50.5, NOON)
    assert not out and why == ""


# --------------------------------------------------------------- rotation


def test_no_rotation_while_the_incumbent_still_qualifies():
    rotate, why = should_rotate(_trade(), _cs(score=80.0), _cs("SOXL", 100.0), 50.5)
    assert not rotate and "still qualifies" in why


def test_no_rotation_out_of_a_profitable_position():
    """Never sell a winner for a better-looking chart."""
    rotate, why = should_rotate(_trade(), _cs(score=40.0, signal="REJECT"),
                                _cs("SOXL", 100.0), 52.0)
    assert not rotate and "profitable" in why


def test_no_rotation_on_a_marginal_score_edge():
    incumbent = _cs(score=70.0, signal="WATCH")
    challenger = _cs("SOXL", 70.0 + ROTATION_MIN_SCORE_MARGIN - 1)
    rotate, why = should_rotate(_trade(), incumbent, challenger, 49.0)
    assert not rotate and "needs" in why


def test_rotation_when_incumbent_fails_and_challenger_is_much_stronger():
    incumbent = _cs(score=50.0, signal="REJECT")
    challenger = _cs("SOXL", 50.0 + ROTATION_MIN_SCORE_MARGIN + 5)
    rotate, why = should_rotate(_trade(), incumbent, challenger, 49.0)
    assert rotate and "no longer qualifies" in why


def test_rotation_handles_a_vanished_incumbent():
    """If the held symbol stops scoring at all, it can still be rotated out."""
    challenger = _cs("SOXL", ROTATION_MIN_SCORE_MARGIN + 10)
    rotate, _ = should_rotate(_trade(), None, challenger, 49.0)
    assert rotate is True
