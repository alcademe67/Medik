"""Live-execution tests: the paths that decide whether a real order goes out.

Everything here is pure or stubbed — no broker is contacted.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass

import pytest

from strategy.medik_etf import (
    ETF_UNIVERSE, PortfolioState, Position, SessionControls, SizingRejected,
    TradeLedger, authorize_order, size_trade,
)
from strategy.medik_etf_ops import (
    WorkingOrder, reconcile_startup, verify_account_mode,
)


def _live():
    if "ib_async" not in sys.modules:
        stub = types.ModuleType("ib_async")
        for n in ("IB", "Stock", "LimitOrder", "MarketOrder", "Trade"):
            setattr(stub, n, type(n, (), {}))
        sys.modules["ib_async"] = stub
    spec = importlib.util.spec_from_file_location(
        "etf_live_exec", "examples/medik_etf_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cs(symbol="TQQQ", score=90.0, price=50.0, atr=1.0):
    from strategy.medik_etf import CandidateScore
    return CandidateScore(symbol, score, "TRADE", price, 60.0, 2.0, atr,
                          price - 1.0, 0.02, True, True, "BULLISH")


# --------------------------------------------------- paper / live separation


def test_paper_requires_d_account_and_port_7497():
    assert verify_account_mode("paper", ["DU123456"], 7497)[0] is True
    assert verify_account_mode("paper", ["U26953060"], 7497)[0] is False
    assert verify_account_mode("paper", ["DU123456"], 7496)[0] is False


def test_live_refuses_a_paper_account():
    ok, why = verify_account_mode("live", ["DU123456"], 7496)
    assert not ok and "paper ids" in why


def test_live_accepts_the_real_account():
    assert verify_account_mode("live", ["U26953060"], 7496)[0] is True


def test_an_unknown_mode_is_refused():
    assert verify_account_mode("simulated", ["U1"], 7496)[0] is False


def test_live_trading_is_not_accepted_as_a_substitute(monkeypatch):
    live = _live()
    monkeypatch.delenv(live.LIVE_ENV_VAR, raising=False)
    monkeypatch.delenv(live.RISK_ACK_ENV_VAR, raising=False)
    monkeypatch.setenv("LIVE_TRADING", "true")
    armed, lines = live.arming_report()
    assert armed is False
    assert any("NOT a recognised" in l for l in lines)


def test_missing_risk_ack_leaves_the_bot_disarmed(monkeypatch):
    live = _live()
    monkeypatch.setenv(live.LIVE_ENV_VAR, "true")
    monkeypatch.delenv(live.RISK_ACK_ENV_VAR, raising=False)
    assert live.arming_report()[0] is False


def test_all_three_keys_arm(monkeypatch):
    live = _live()
    monkeypatch.setenv(live.LIVE_ENV_VAR, "true")
    monkeypatch.setenv(live.RISK_ACK_ENV_VAR, "true")
    assert live.arming_report()[0] is True


# ------------------------------------------------------------ unmanaged QQQ


def test_unmanaged_qqq_blocks_entries_but_lets_the_loop_run():
    """The reported requirement: detect it, refuse NEW ENTRIES, keep running."""
    d = reconcile_startup([Position("QQQ", 0.2836, 203.11)], [], ETF_UNIVERSE)
    assert d.action == "BLOCK_ENTRIES"
    assert d.may_run is True          # loop stays up, kill switch live
    assert d.may_trade is False       # no new positions
    assert "QQQ" in d.unmanaged


def test_unmanaged_position_is_reported_verbatim():
    d = reconcile_startup([Position("QQQ", 0.2836, 203.11)], [], ETF_UNIVERSE)
    joined = " ".join(d.notes)
    assert "UNMANAGED POSITION: QQQ" in joined
    assert "ACTION REQUIRED" in joined


def test_the_position_is_never_hidden():
    d = reconcile_startup([Position("QQQ", 0.2836, 203.11)], [], ETF_UNIVERSE)
    assert any("QQQ" in n for n in d.notes)


# ------------------------------------------- bracket + restart recovery


def test_a_bracketed_position_is_adopted_after_restart():
    pos = Position("TQQQ", 5, 350.0)
    orders = [WorkingOrder("TQQQ", "SELL", "STP", 5, 68.0),
              WorkingOrder("TQQQ", "SELL", "LMT", 5, 75.0)]
    d = reconcile_startup([pos], orders, ETF_UNIVERSE)
    assert d.action == "ADOPT" and d.may_trade
    assert d.adopted.stop == 68.0 and d.adopted.target == 75.0
    assert d.adopted.entry == pytest.approx(70.0)


def test_restart_never_assumes_flat_just_because_open_trade_is_none():
    """open_trade starts as None on every restart; the broker is the truth."""
    d = reconcile_startup([Position("TQQQ", 5, 350.0)], [], ETF_UNIVERSE)
    assert d.action != "START"
    assert d.may_trade is False


def test_a_half_bracket_is_not_adopted():
    orders = [WorkingOrder("TQQQ", "SELL", "STP", 5, 68.0)]
    d = reconcile_startup([Position("TQQQ", 5, 350.0)], orders, ETF_UNIVERSE)
    assert d.action == "BLOCK_ENTRIES"


# --------------------------------------------------------- STOP_MEDIK


def test_stop_medik_is_detected(tmp_path):
    from strategy.medik_etf_ops import kill_switch_active
    p = tmp_path / "STOP_MEDIK"
    assert kill_switch_active(p) is False
    p.write_text("")
    assert kill_switch_active(p) is True


def test_shutdown_cancels_before_flattening():
    """Order matters: a resting stop is a live SELL. Flattening alongside it
    can fill both and leave the account short."""
    src = open("examples/medik_etf_live.py").read()
    body = src.split("def emergency_shutdown")[1].split("\ndef ")[0]
    assert body.index("cancelling working orders") < body.index("flattening open position")
    assert body.index("flattening open position") < body.index("confirming flat")


# ------------------------------------------------- duplicate + sizing


def test_duplicate_entry_is_blocked():
    ledger = TradeLedger()
    ledger.mark_entered("TQQQ", 1000.0)
    state = PortfolioState(10_000.0, 10_000.0, (), 0)
    cand = _cs()
    auth = authorize_order(
        live_enabled=True, connected=True, state=state,
        controls=SessionControls(equity_start_of_session=10_000.0),
        candidate=cand, sized=size_trade(cand, state), now_minutes=720,
        ledger=ledger, now_ts=1030.0)
    assert not auth and "not_duplicate" in auth.failures


def test_quantity_below_one_skips_with_the_exact_reason():
    state = PortfolioState(290.0, 87.21, (), 0)
    with pytest.raises(SizingRejected) as exc:
        size_trade(_cs(price=500.0, atr=10.0), state)
    msg = str(exc.value)
    assert "SKIP: quantity below 1 whole share" in msg
    assert "Binding cap:" in msg
    for cap in ("risk_per_trade", "position_notional", "available_cash", "ndx_exposure"):
        assert cap in msg


def test_a_valid_order_authorizes_for_real_submission():
    """The success path: every check passes, so a real bracket may be sent."""
    state = PortfolioState(10_000.0, 10_000.0, (), 0)
    cand = _cs()
    auth = authorize_order(
        live_enabled=True, connected=True, state=state,
        controls=SessionControls(equity_start_of_session=10_000.0),
        candidate=cand, sized=size_trade(cand, state), now_minutes=720,
        ledger=TradeLedger(), now_ts=1000.0)
    assert auth.authorized and auth.failures == ()


def test_orders_are_real_not_simulated():
    """No fake-fill path may exist in the live runner.

    Checks EXECUTABLE lines only. The word "simulated" legitimately appears
    in a log line explaining that IBKR's paper account simulates fills --
    which is true, and is the broker doing it, not this code.
    """
    lines = []
    for raw in open("examples/medik_etf_live.py"):
        stripped = raw.strip()
        if stripped.startswith("#") or stripped.startswith("log("):
            continue
        lines.append(raw)
    code = "".join(lines).lower()

    assert "ib.placeorder" in code, "the runner must actually submit orders"
    for fake in ("fake_fill", "dry_fill", "pretend_fill", "mock_order",
                 "simulate_fill", "simulated_fill"):
        assert fake not in code, f"simulation path {fake} present"


def test_paper_mode_does_not_short_circuit_submission():
    """Paper fills come from IBKR's paper account, not from this program.

    There must be no branch that skips placeOrder and invents a result --
    that would make a paper run prove nothing about the live path.
    """
    src = open("examples/medik_etf_live.py").read()
    place_calls = src.count("ib.placeOrder")
    assert place_calls >= 1
    # placeOrder must not sit behind a paper/live conditional
    for line in src.splitlines():
        if "ib.placeOrder" in line:
            assert "paper" not in line.lower()
