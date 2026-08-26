"""Dry run: the whole pipeline on live quotes, with nothing submitted.

The point is to answer "would it have traded, and with what?" before any
money is involved. That only means something if the dry run exercises the
SAME code the live path does — same scoring, same sizing, same authorization
checklist — and stops at exactly one place: the call to placeOrder.
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
        "etf_live_dry", "examples/medik_etf_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SRC = open("examples/medik_etf_live.py").read()


def _scan_body() -> str:
    return SRC.split("def scan_once")[1].split("\ndef ")[0]


# --------------------------------------------------------------- the gate


def test_dry_run_is_exact_match_like_the_live_gate(monkeypatch):
    live = _live()
    for value in ("", "TRUE", "True", "1", "yes", "on", "false"):
        monkeypatch.setenv(live.DRY_RUN_ENV_VAR, value)
        assert live.dry_run() is False, value
    monkeypatch.setenv(live.DRY_RUN_ENV_VAR, "true")
    assert live.dry_run() is True


def test_dry_run_unset_is_off(monkeypatch):
    live = _live()
    monkeypatch.delenv(live.DRY_RUN_ENV_VAR, raising=False)
    assert live.dry_run() is False


def test_dry_run_off_does_not_by_itself_arm_anything(monkeypatch):
    """Turning dry run OFF must not be a way to start trading. Both live keys
    are still required."""
    live = _live()
    monkeypatch.delenv(live.DRY_RUN_ENV_VAR, raising=False)
    monkeypatch.delenv(live.LIVE_ENV_VAR, raising=False)
    monkeypatch.delenv(live.RISK_ACK_ENV_VAR, raising=False)
    assert live.arming_report()[0] is False


# ------------------------------------------------- where it stops, exactly


def test_the_dry_run_exit_is_after_the_risk_checks():
    """Stopping earlier would mean the checks were never tested; the dry run
    would prove nothing about whether a real order would have passed."""
    body = _scan_body()
    assert body.index("authorize_order(") < body.index("if dry_run():")
    assert body.index('log(f"RISK | PASS') < body.index("if dry_run():")


def test_the_dry_run_exit_is_before_the_order():
    body = _scan_body()
    assert body.index("if dry_run():") < body.index("place_bracket(")


def test_the_dry_run_exit_is_before_the_ledger_is_marked():
    """Marking an entry that never happened would block the symbol from a
    real trade later by duplicate suppression."""
    body = _scan_body()
    assert body.index("if dry_run():") < body.index("ledger.mark_pending(")


def test_a_dry_run_returns_no_open_trade():
    """It must not report a position it did not take."""
    body = _scan_body()
    after = body[body.index("if dry_run():"):]
    first_return = after[:after.index("ledger.mark_pending(")]
    assert "return None" in first_return


# ------------------------------------------------------ it cannot be silent


def test_the_banner_says_dry_run_instead_of_enabled(monkeypatch):
    """A dry run must never be mistakable for a live one in the log."""
    assert 'AUTOMATIC TRADING: DRY RUN' in SRC
    assert "NO ORDERS WILL BE SENT" in SRC


def test_the_skipped_order_is_logged_with_its_parameters():
    body = _scan_body()
    line = body[body.index("if dry_run():"):][:400]
    for token in ("DRY RUN", "would submit BUY", "sized.quantity",
                  "sized.symbol", "NO ORDER SENT"):
        assert token in line, token


# ------------------------------------------------- structured log format


@pytest.mark.parametrize("tag", ["DATA |", "RANK |", "SIGNAL |", "RISK | PASS",
                                 "RISK | FAIL", "NO TRADE |", "DRY RUN |"])
def test_every_requested_log_tag_is_emitted(tag):
    assert tag in SRC, f"{tag} never logged"


def test_no_trade_always_carries_a_reason():
    """A silent skip is indistinguishable from a bug at 6:45 in the morning."""
    for match in re.finditer(r'NO TRADE \| ([^"]*)', SRC):
        assert "reason=" in match.group(1), match.group(0)


def test_data_lines_carry_price_volume_and_score():
    """The log line is one f-string split across source lines, so join them
    before checking -- testing only the first line would pass or fail on
    where the formatter happened to wrap."""
    lines = SRC.splitlines()
    start = next(i for i, l in enumerate(lines) if "DATA |" in l)
    joined = " ".join(lines[start:start + 4])
    for field in ("price=", "rvol=", "score=", "rsi="):
        assert field in joined, field


# -------------------------------------------- nothing else was weakened


def test_the_live_path_still_calls_place_bracket():
    assert "place_bracket(ib, contracts[sized.symbol]" in SRC


def test_risk_constants_untouched():
    import strategy.medik_etf as m
    assert (m.RISK_PCT_DEFAULT, m.RISK_PCT_MAX) == (0.5, 1.0)
    assert (m.MAX_ACTIVE_POSITIONS, m.MAX_TRADES_PER_SESSION) == (1, 3)
    assert m.MAX_DAILY_LOSS_PCT == 2.0
