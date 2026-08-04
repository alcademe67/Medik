"""Validates OHLCV data before it's used for scoring.

Root cause this guards against (found 2026-08-03): a daily-bar fetch
captured mid-session by an interrupted/retried scan got cached to disk and
was reused hours later without ever being refreshed. "Today's" bar silently
held partial-day volume (~30% of the real total) long after the actual
session had closed with a different close and 3x the volume. The gate
evaluated that stale bar as a volume-confirmation failure when the real,
complete bar would have shown something else entirely.

Two independent checks, both required before a bar feeds the scoring engine:

1. validate_freshness -- trusts IBKR's own `expires` field on a
   get_price_history response. If more time has passed than IBKR itself
   vouches for the snapshot, the caller must refetch before scoring.
2. last_bar_is_complete -- a daily bar for trading day D is only "complete"
   once the US regular session (9:30-16:00 America/New_York) for that date
   has closed, checked against wall-clock time in that timezone rather than
   a UTC-hour heuristic (which is what broke here: UTC date rolling over
   made a genuinely-fetched-mid-session bar look "old enough" to trust).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
SESSION_CLOSE_HOUR = 16
SESSION_CLOSE_MINUTE = 5  # small settle buffer past the 16:00 close


class StaleDataError(Exception):
    """Raised when cached/fetched price data can't be trusted for scoring."""


def validate_freshness(payload: dict, now: datetime | None = None) -> None:
    """payload is the raw JSON dict returned by get_price_history (must
    include an 'expires' timestamp). Raises StaleDataError if more wall-clock
    time has passed than IBKR's own snapshot vouches for -- i.e. this
    response (or a file saved from it) is old enough that a fresh call could
    legitimately return different numbers.
    """
    now = now or datetime.now(timezone.utc)
    expires_raw = payload.get("expires")
    if not expires_raw:
        raise StaleDataError("payload has no 'expires' field -- can't verify freshness")
    expires = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
    if now > expires:
        raise StaleDataError(
            f"data expired at {expires_raw}, now is {now.isoformat()} -- refetch before scoring"
        )


def last_bar_is_complete(bar_date: date, now: datetime | None = None) -> bool:
    """True once the US regular session for bar_date has closed, evaluated
    in America/New_York wall-clock time (not a UTC-hour proxy). Does not
    account for market holidays -- good enough for a same-day completeness
    check, not a full trading calendar.
    """
    now = now or datetime.now(timezone.utc)
    now_et = now.astimezone(EASTERN)
    if bar_date < now_et.date():
        return True
    if bar_date > now_et.date():
        return False
    close_time = now_et.replace(
        hour=SESSION_CLOSE_HOUR, minute=SESSION_CLOSE_MINUTE, second=0, microsecond=0
    )
    return now_et >= close_time


def drop_incomplete_trailing_bar(df, now: datetime | None = None):
    """Given a DataFrame indexed by bar date (ascending), drop the last row
    if its trading day's session hasn't closed yet. Returns (df, dropped)."""
    if df.empty:
        return df, False
    last_date = df.index[-1].date() if hasattr(df.index[-1], "date") else df.index[-1]
    if last_bar_is_complete(last_date, now):
        return df, False
    return df.iloc[:-1], True
