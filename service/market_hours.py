"""US regular-session market-hours check, in America/New_York wall-clock
time (not a UTC-hour proxy -- see strategy/data_quality.py for why that
matters). Does not account for market holidays; good enough to gate a
polling loop, not a full trading calendar.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from service.config import ServiceConfig

EASTERN = ZoneInfo("America/New_York")


def market_is_open(config: ServiceConfig, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    now_et = now.astimezone(EASTERN)
    if now_et.weekday() >= 5:  # Sat/Sun
        return False
    open_time = now_et.replace(
        hour=config.market_open_hour_et, minute=config.market_open_minute_et, second=0, microsecond=0
    )
    close_time = now_et.replace(
        hour=config.market_close_hour_et, minute=config.market_close_minute_et, second=0, microsecond=0
    )
    return open_time <= now_et < close_time
