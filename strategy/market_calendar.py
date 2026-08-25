"""US market holidays and early closes, for a bot that runs unattended.

WHY THIS EXISTS
    _market_open() checked weekday and clock time only, so on Thanksgiving it
    saw "Thursday, 10:30" and scanned as usual. With a human starting the bot
    each morning that was harmless -- nobody runs it on a holiday. Under a
    scheduled task it fires every weekday whether or not the market exists
    that day.

    The quote check is still the primary guard: with no live bid/ask,
    read_quote() refuses everything and no trade can be sized. This calendar
    is defence in depth, and it turns a session of confusing "no usable quote"
    lines into one clear line saying why.

EARLY CLOSES
    On a half day the session ends at 13:00 ET, so the 30-minute no-entry
    buffer has to move with it. Otherwise the bot would keep taking entries
    for three hours after the close, on quotes that stopped updating.

COVERAGE
    Hardcoded tables expire, and one that expires silently is worse than none
    at all -- it would report every future holiday as a normal trading day.
    COVERAGE_END is therefore explicit and coverage_warning() says so out
    loud. Refresh from the NYSE published calendar, not from memory.

    These dates are transcribed from the NYSE holiday schedule. The full
    closures are the standard ten per year; the early closes are the ones
    that recur predictably. Verify against nyse.com before relying on a
    half-day: the rules for holidays falling on a weekend vary, and this
    table is worth exactly as much as its last check.
"""
from __future__ import annotations

from datetime import date

# Full closures -- the market does not open at all.
MARKET_HOLIDAYS: frozenset = frozenset({
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Washington's Birthday
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day observed (Jul 4 is a Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
    # 2027
    date(2027, 1, 1),    # New Year's Day
    date(2027, 1, 18),   # Martin Luther King Jr. Day
    date(2027, 2, 15),   # Washington's Birthday
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth observed (Jun 19 is a Saturday)
    date(2027, 7, 5),    # Independence Day observed (Jul 4 is a Sunday)
    date(2027, 9, 6),    # Labor Day
    date(2027, 11, 25),  # Thanksgiving
    date(2027, 12, 24),  # Christmas observed (Dec 25 is a Saturday)
})

# Sessions that close at 13:00 ET instead of 16:00.
EARLY_CLOSES: frozenset = frozenset({
    date(2026, 11, 27),  # day after Thanksgiving
    date(2026, 12, 24),  # Christmas Eve
    date(2027, 11, 26),  # day after Thanksgiving
})

EARLY_CLOSE_MINUTES = 13 * 60
REGULAR_CLOSE_MINUTES = 16 * 60

# The last date these tables are known to describe.
COVERAGE_END = date(2027, 12, 31)


def is_market_holiday(day: date) -> bool:
    return day in MARKET_HOLIDAYS


def is_early_close(day: date) -> bool:
    return day in EARLY_CLOSES


def close_minutes(day: date) -> int:
    """Minutes past midnight ET at which the session ends."""
    return EARLY_CLOSE_MINUTES if is_early_close(day) else REGULAR_CLOSE_MINUTES


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and not is_market_holiday(day)


def coverage_warning(day: date) -> str:
    """A warning when `day` is past what the tables describe, else "".

    Returned rather than raised: a bot that refuses to run because its
    calendar is stale is a bot that stops trading on a date nobody chose.
    The caller logs this and carries on, with the quote check still standing
    between a stale calendar and a bad trade.
    """
    if day > COVERAGE_END:
        return (f"market calendar covers dates up to {COVERAGE_END}; {day} is "
                "beyond it, so holidays and early closes are NOT being checked "
                "— refresh strategy/market_calendar.py from the NYSE schedule")
    return ""


def describe(day: date) -> str:
    """One line about the session on `day`, for the log."""
    if day.weekday() >= 5:
        return f"{day} is a weekend — market closed"
    if is_market_holiday(day):
        return f"{day} is a US market holiday — market closed all day"
    if is_early_close(day):
        return f"{day} is an early close — session ends 13:00 ET, not 16:00"
    return f"{day} is a regular session (09:30–16:00 ET)"
