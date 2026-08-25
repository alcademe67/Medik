"""Running on a schedule with nobody watching.

Every failure here used to be visible: a human ran the command, saw the
traceback, and started TWS. Under Task Scheduler the same failure is silent,
so each one has to announce itself in a file and exit with a code that says
which failure it was.

The rule these tests pin: a failed connection, a bad account, stale data or a
failed preflight must all mean NO ORDER. Waiting longer for TWS must not
relax anything downstream.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import types

import pytest


def _live():
    if "ib_async" not in sys.modules:
        stub = types.ModuleType("ib_async")
        for n in ("IB", "Stock", "LimitOrder", "MarketOrder", "Trade"):
            setattr(stub, n, type(n, (), {}))
        sys.modules["ib_async"] = stub
    spec = importlib.util.spec_from_file_location(
        "etf_live_unattended", "examples/medik_etf_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeClient:
    """Fails `failures` times, then connects — or never, if failures is None."""

    port = 7496

    def __init__(self, failures=0, connects=True):
        self.failures, self.connects = failures, connects
        self.attempts = 0

    def connect(self, timeout=15, retries=0):
        self.attempts += 1
        if self.failures is None or self.attempts <= self.failures:
            raise ConnectionRefusedError("[Errno 111] Connection refused")
        return types.SimpleNamespace(isConnected=lambda: self.connects)


# ------------------------------------------------------ waiting for TWS


def test_it_waits_for_tws_rather_than_dying_on_the_first_refusal(monkeypatch):
    """6:45 is a fixed time; TWS restarts on its own schedule. Some mornings
    the bot arrives first, and six seconds of retries is not enough."""
    live = _live()
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    monkeypatch.setattr(live, "CONNECT_RETRY_SECONDS", 0.01)
    client = FakeClient(failures=4)
    ib = live.connect_with_wait(client, deadline_minutes=5)
    assert ib is not None
    assert client.attempts == 5


def test_it_gives_up_and_returns_none_when_tws_never_comes(monkeypatch):
    live = _live()
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    monkeypatch.setattr(live, "CONNECT_RETRY_SECONDS", 0.01)
    assert live.connect_with_wait(FakeClient(failures=None),
                                  deadline_minutes=0.02) is None


def test_a_disconnected_client_is_not_treated_as_connected(monkeypatch):
    """connect() returning an object is not proof of a live socket."""
    live = _live()
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    monkeypatch.setattr(live, "CONNECT_RETRY_SECONDS", 0.01)
    assert live.connect_with_wait(FakeClient(failures=0, connects=False),
                                  deadline_minutes=0.02) is None


def test_giving_up_returns_rather_than_raising():
    """A traceback in a discarded stdout says nothing. The caller logs a
    reason and exits with a code Task Scheduler can show.

    Checks EXECUTABLE lines only: the docstring legitimately contains the
    word "raises" while explaining that it does not.
    """
    import ast
    import inspect

    live = _live()
    tree = ast.parse(inspect.getsource(live.connect_with_wait))
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]
    returns = [n for n in ast.walk(tree) if isinstance(n, ast.Return)]
    assert any(isinstance(r.value, ast.Constant) and r.value.value is None
               for r in returns), "must return None when it gives up"


# -------------------------------------------------- no connection, no order


def test_no_tws_exits_before_anything_can_be_ordered():
    live = _live()
    src = open("examples/medik_etf_live.py").read()
    body = src.split("def main(")[1]
    no_tws = body.index("return EXIT_NO_TWS")
    assert no_tws < body.index("scan_once("), "must exit before any scan"
    assert live.EXIT_NO_TWS != 0, "a failed morning must not report success"


def test_connection_is_still_an_independent_authorization_check():
    """Waiting longer to connect must not remove the check that we ARE
    connected at the moment an order is authorised."""
    from strategy.medik_etf import (
        ETF_UNIVERSE, PortfolioState, SessionControls, TradeLedger,
        authorize_order, size_trade,
    )
    from strategy.medik_etf import CandidateScore
    cand = CandidateScore("TQQQ", 90.0, "TRADE", 50.0, 60.0, 2.0, 1.0,
                          49.0, 0.02, True, True, "BULLISH")
    state = PortfolioState(10_000.0, 10_000.0, (), 0)
    kw = dict(state=state, controls=SessionControls(equity_start_of_session=10_000.0),
              candidate=cand, sized=size_trade(cand, state), now_minutes=720,
              ledger=TradeLedger(), now_ts=1000.0)
    assert authorize_order(live_enabled=True, connected=True, **kw).authorized
    denied = authorize_order(live_enabled=True, connected=False, **kw)
    assert not denied and "ibkr_connected" in denied.failures


def test_preflight_and_incoherent_state_have_their_own_exit_codes():
    live = _live()
    codes = {live.EXIT_NO_TWS, live.EXIT_PREFLIGHT, live.EXIT_INCOHERENT}
    assert len(codes) == 3, "distinct codes, or the log cannot say which failed"
    assert 0 not in codes


def test_the_kill_switch_is_checked_before_connecting():
    """If the operator stopped the bot, that outranks everything below."""
    src = open("examples/medik_etf_live.py").read()
    body = src.split("def main(")[1]
    assert body.index("kill_switch_active()") < body.index("connect_with_wait")


# ------------------------------------------------------------- logging


def test_every_line_goes_to_a_dated_file(tmp_path, monkeypatch):
    live = _live()
    monkeypatch.setattr(live, "LOG_DIR", tmp_path / "logs")
    live.log("preflight PASS")
    live.log("SIGNAL TQQQ score 88")
    [written] = list((tmp_path / "logs").glob("medik_etf_*.log"))
    text = written.read_text()
    assert "preflight PASS" in text and "SIGNAL TQQQ score 88" in text


def test_log_lines_carry_the_date_not_just_the_time(tmp_path, monkeypatch):
    """A file spanning several runs needs the date on the line."""
    live = _live()
    monkeypatch.setattr(live, "LOG_DIR", tmp_path / "logs")
    live.log("hello")
    [written] = list((tmp_path / "logs").glob("medik_etf_*.log"))
    assert re.match(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", written.read_text())


def test_a_broken_log_directory_does_not_stop_the_bot(tmp_path, monkeypatch, capsys):
    """A disk problem must not kill a loop that is holding a position."""
    live = _live()
    blocker = tmp_path / "logs"
    blocker.write_text("not a directory")
    monkeypatch.setattr(live, "LOG_DIR", blocker)
    live.log("still running")
    assert "still running" in capsys.readouterr().out


# ------------------------------------------- risk controls are untouched


def test_no_risk_constant_was_relaxed_for_automation():
    import strategy.medik_etf as m
    assert m.RISK_PCT_DEFAULT == 0.5
    assert m.RISK_PCT_MAX == 1.0
    assert m.MAX_ACTIVE_POSITIONS == 1
    assert m.MAX_TRADES_PER_SESSION == 3
    assert m.MAX_DAILY_LOSS_PCT == 2.0
    assert m.MAX_CAPITAL_UTILIZATION == 0.90
    assert m.OPEN_DELAY_MIN == 15
    assert m.CLOSE_BUFFER_MIN == 30


def test_the_authorization_checklist_still_has_every_gate():
    import inspect

    from strategy.medik_etf import authorize_order
    src = inspect.getsource(authorize_order)
    for gate in ("live_mode_enabled", "ibkr_connected", "account_data_valid",
                 "buying_power_valid", "market_data_valid", "setup_valid",
                 "position_size_valid", "whole_share_quantity", "stop_valid",
                 "target_valid", "reward_risk_ok", "risk_within_limit",
                 "capital_utilization_ok", "affordable",
                 "no_conflicting_position", "no_conflicting_open_order",
                 "not_duplicate", "session_gates_ok", "entries_enabled"):
        assert f'checks["{gate}"]' in src, f"{gate} gate is missing"


def test_the_wait_loop_is_bounded_by_attempts_as_well_as_time(monkeypatch):
    """The deadline only advances because we sleep. A suppressed or very
    short sleep must not turn the wait into a spin that fills the log."""
    live = _live()
    monkeypatch.setattr(live.time, "sleep", lambda s: None)   # clock frozen
    monkeypatch.setattr(live, "CONNECT_RETRY_SECONDS", 0.001)
    client = FakeClient(failures=None)
    assert live.connect_with_wait(client, deadline_minutes=20) is None
    assert client.attempts < 5000, "unbounded retry loop"
