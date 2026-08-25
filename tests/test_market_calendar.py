"""Holidays and half-days, which only start mattering once nobody is watching.

_market_open() checked weekday and clock only. A human starting the bot each
morning never runs it on Thanksgiving; a scheduled task fires every weekday
whether the market exists that day or not.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from strategy.market_calendar import (
    COVERAGE_END, EARLY_CLOSE_MINUTES, REGULAR_CLOSE_MINUTES,
    close_minutes, coverage_warning, describe, is_early_close,
    is_market_holiday, is_trading_day,
)
from strategy.medik_etf import PortfolioState, SessionControls, check_can_enter

NY = ZoneInfo("America/New_York")


def _live():
    if "ib_async" not in sys.modules:
        stub = types.ModuleType("ib_async")
        for n in ("IB", "Stock", "LimitOrder", "MarketOrder", "Trade"):
            setattr(stub, n, type(n, (), {}))
        sys.modules["ib_async"] = stub
    spec = importlib.util.spec_from_file_location(
        "etf_live_cal", "examples/medik_etf_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------- the tables


@pytest.mark.parametrize("day", [
    date(2026, 1, 1), date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
    date(2026, 7, 3), date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 3, 26), date(2027, 6, 18), date(2027, 7, 5), date(2027, 12, 24),
])
def test_known_closures_are_not_trading_days(day):
    assert is_market_holiday(day)
    assert not is_trading_day(day)


def test_every_listed_holiday_is_a_weekday():
    """A weekend entry would be dead weight and hint the table is wrong."""
    from strategy.market_calendar import MARKET_HOLIDAYS
    for day in MARKET_HOLIDAYS:
        assert day.weekday() < 5, f"{day} is a {day.strftime('%A')}"


def test_observed_dates_land_where_the_weekend_pushes_them():
    """Jul 4 2026 is a Saturday, so the market closes Friday the 3rd."""
    assert is_market_holiday(date(2026, 7, 3))
    assert not is_market_holiday(date(2026, 7, 4))    # Saturday anyway
    # Jun 19 2027 is a Saturday -> observed Friday the 18th
    assert is_market_holiday(date(2027, 6, 18))


def test_an_ordinary_weekday_trades():
    assert is_trading_day(date(2026, 8, 26))
    assert close_minutes(date(2026, 8, 26)) == REGULAR_CLOSE_MINUTES


def test_a_weekend_is_never_a_trading_day():
    assert not is_trading_day(date(2026, 8, 29))     # Saturday
    assert not is_trading_day(date(2026, 8, 30))     # Sunday


# ------------------------------------------------------- early closes


def test_a_half_day_still_trades_but_ends_early():
    day = date(2026, 11, 27)                         # day after Thanksgiving
    assert is_trading_day(day) and is_early_close(day)
    assert close_minutes(day) == EARLY_CLOSE_MINUTES == 13 * 60


def test_no_early_close_is_also_a_holiday():
    """A date cannot be both; that would mean the table contradicts itself."""
    from strategy.market_calendar import EARLY_CLOSES, MARKET_HOLIDAYS
    assert not (EARLY_CLOSES & MARKET_HOLIDAYS)


def test_the_entry_buffer_moves_with_the_close():
    """12:35 ET is inside a normal session but past the cutoff on a half day."""
    state = PortfolioState(10_000.0, 10_000.0, (), 0)
    controls = SessionControls(equity_start_of_session=10_000.0)
    at_1235 = 12 * 60 + 35

    ok, _ = check_can_enter(state, controls, at_1235)
    assert ok, "12:35 is a normal trading time on a full session"

    ok, why = check_can_enter(state, controls, at_1235,
                              close_minutes=EARLY_CLOSE_MINUTES)
    assert not ok and "close" in why, (
        "a fixed 16:00 cutoff would keep taking entries for three hours "
        "after a 13:00 close, on quotes that stopped updating")


def test_the_default_close_is_unchanged_for_existing_callers():
    state = PortfolioState(10_000.0, 10_000.0, (), 0)
    controls = SessionControls(equity_start_of_session=10_000.0)
    assert check_can_enter(state, controls, 12 * 60)[0] is True
    assert check_can_enter(state, controls, 15 * 60 + 45)[0] is False


# ------------------------------------------------------------ coverage


def test_a_date_inside_coverage_warns_about_nothing():
    assert coverage_warning(COVERAGE_END) == ""


def test_a_date_past_coverage_says_so():
    """A hardcoded table that expires silently is worse than none: it would
    report every future holiday as an ordinary trading day."""
    warning = coverage_warning(date(2028, 3, 1))
    assert "beyond it" in warning and "NYSE" in warning


def test_coverage_warns_rather_than_raising():
    """Refusing to run on a stale calendar would stop trading on a date
    nobody chose. The quote check still stands behind it."""
    assert isinstance(coverage_warning(date(2030, 1, 1)), str)


def test_describe_names_the_session_type():
    assert "holiday" in describe(date(2026, 11, 26))
    assert "early close" in describe(date(2026, 11, 27))
    assert "weekend" in describe(date(2026, 8, 29))
    assert "regular session" in describe(date(2026, 8, 26))


# ------------------------------------------------- the runner's gate


def test_the_runner_is_closed_on_a_holiday():
    live = _live()
    thanksgiving = datetime(2026, 11, 26, 10, 30, tzinfo=NY)
    assert live._market_open(thanksgiving) is False


def test_the_runner_is_open_on_an_ordinary_morning():
    live = _live()
    assert live._market_open(datetime(2026, 8, 26, 10, 30, tzinfo=NY)) is True


def test_the_runner_closes_at_1300_on_a_half_day():
    live = _live()
    half = date(2026, 11, 27)
    assert live._market_open(datetime(half.year, half.month, half.day, 12, 30,
                                      tzinfo=NY)) is True
    assert live._market_open(datetime(half.year, half.month, half.day, 13, 30,
                                      tzinfo=NY)) is False
    # ... and would have been "open" under the old weekday-and-clock rule
    assert 9 * 60 + 30 <= 13 * 60 + 30 < 16 * 60


def test_the_runner_is_closed_at_the_weekend():
    live = _live()
    assert live._market_open(datetime(2026, 8, 29, 10, 30, tzinfo=NY)) is False
