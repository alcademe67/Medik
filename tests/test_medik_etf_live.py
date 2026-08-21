"""Tests for the ETF live runner's gates (examples/medik_etf_live.py).

ib_async is stubbed so the module can be imported without a broker or the
package installed. These cover the parts that decide whether an order is
allowed to exist at all: the live-mode flag, market-hours logic, and the
protection verification that stands between a fill and an unprotected
position.
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
import types
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pytest

# ---- stub ib_async before importing the runner
if "ib_async" not in sys.modules:
    stub = types.ModuleType("ib_async")
    for name in ("IB", "Stock", "LimitOrder", "MarketOrder", "Trade"):
        setattr(stub, name, type(name, (), {}))
    sys.modules["ib_async"] = stub

_spec = importlib.util.spec_from_file_location(
    "medik_etf_live", "examples/medik_etf_live.py")
live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(live)

NY = ZoneInfo("America/New_York")


# ------------------------------------------------------------ live-mode gate


def test_live_disabled_when_unset(monkeypatch):
    monkeypatch.delenv(live.LIVE_ENV_VAR, raising=False)
    assert live.live_enabled() is False


@pytest.mark.parametrize("value", ["", "false", "TRUE", "True", "1", "yes", "on", " true "])
def test_live_requires_exactly_true(monkeypatch, value):
    """Never inferred, never truthy-ish. Only the exact string arms it."""
    monkeypatch.setenv(live.LIVE_ENV_VAR, value)
    assert live.live_enabled() is False


def test_live_enabled_on_exact_match(monkeypatch):
    monkeypatch.setenv(live.LIVE_ENV_VAR, "true")
    assert live.live_enabled() is True


def test_env_var_name_is_the_documented_one():
    assert live.LIVE_ENV_VAR == "MEDIK_ETF_LIVE"


# ------------------------------------------------------------- market hours


@pytest.mark.parametrize(
    "when, expected",
    [
        ((2026, 8, 24, 11, 0), True),    # Monday midday
        ((2026, 8, 24, 9, 29), False),   # before the open
        ((2026, 8, 24, 16, 0), False),   # at the close
        ((2026, 8, 22, 11, 0), False),   # Saturday
        ((2026, 8, 23, 11, 0), False),   # Sunday
    ],
)
def test_market_open_detection(when, expected):
    assert live._market_open(datetime.datetime(*when, tzinfo=NY)) is expected


def test_now_minutes():
    assert live._now_minutes(datetime.datetime(2026, 8, 24, 9, 45, tzinfo=NY)) == 585


# -------------------------------------------------- bracket protection check


@dataclass
class _Status:
    status: str
    filled: float = 0.0


@dataclass
class _Leg:
    orderStatus: _Status


class _FakeIB:
    def sleep(self, _seconds):
        return None


def test_protection_confirmed_when_all_three_legs_live():
    legs = [_Leg(_Status("Filled", 5)), _Leg(_Status("PreSubmitted")),
            _Leg(_Status("PreSubmitted"))]
    ok, why = live.verify_protection(_FakeIB(), legs)
    assert ok and why == ""


@pytest.mark.parametrize("bad_status", ["Cancelled", "ApiCancelled", "Inactive"])
def test_a_dead_protective_leg_is_a_failure(bad_status):
    legs = [_Leg(_Status("Filled", 5)), _Leg(_Status(bad_status)),
            _Leg(_Status("PreSubmitted"))]
    ok, why = live.verify_protection(_FakeIB(), legs)
    assert not ok
    assert "stop" in why and bad_status in why


def test_a_leg_with_no_status_is_a_failure():
    legs = [_Leg(_Status("Filled", 5)), _Leg(_Status("PreSubmitted")),
            _Leg(_Status(""))]
    ok, why = live.verify_protection(_FakeIB(), legs)
    assert not ok and "target" in why


def test_dead_parent_is_also_caught():
    legs = [_Leg(_Status("Cancelled")), _Leg(_Status("PreSubmitted")),
            _Leg(_Status("PreSubmitted"))]
    ok, why = live.verify_protection(_FakeIB(), legs)
    assert not ok and "parent" in why


# ------------------------------------------------- bracket failure disables


def test_bracket_failure_disables_further_entries():
    from strategy.medik_etf import PortfolioState, SessionControls, check_can_enter

    controls = SessionControls(equity_start_of_session=1000.0)
    controls.disable("bracket failure: stop leg is Cancelled")
    ok, why = check_can_enter(PortfolioState(1000.0, 1000.0, ()), controls, 12 * 60)
    assert not ok
    assert "bracket failure" in why
