"""Retry helpers for long-running, unattended polling loops.

A monitor left running with the screen off still loses the odd request:
the wifi radio hiccups, a captive portal reappears, KuCoin rate-limits.
None of that should kill a loop that is meant to run for days, so
``retry_call`` absorbs transient failures with exponential backoff and
only gives up after ``attempts`` tries.
"""

from __future__ import annotations

import time as _time
from typing import Callable, Iterable, Optional, Tuple, TypeVar

import requests

from .client import KuCoinAPIError

T = TypeVar("T")

# Network-level failures plus KuCoin's own error envelope. A signing or
# permission error raises KuCoinAPIError too, but retrying it is cheap
# compared to dropping out of an overnight monitor.
TRANSIENT_ERRORS: Tuple[type, ...] = (
    requests.exceptions.RequestException,
    KuCoinAPIError,
    OSError,
)


def backoff_delays(
    attempts: int, base: float = 2.0, factor: float = 2.0, cap: float = 60.0
) -> list:
    """Delays to wait between ``attempts`` tries: base, base*factor, ... capped."""
    delays = []
    delay = base
    for _ in range(max(attempts - 1, 0)):
        delays.append(min(delay, cap))
        delay *= factor
    return delays


def retry_call(
    fn: Callable[[], T],
    attempts: int = 5,
    base: float = 2.0,
    factor: float = 2.0,
    cap: float = 60.0,
    exceptions: Iterable[type] = TRANSIENT_ERRORS,
    on_error: Optional[Callable[[int, BaseException, float], None]] = None,
    sleep: Callable[[float], None] = _time.sleep,
) -> T:
    """Call ``fn`` until it succeeds, backing off between failures.

    ``on_error`` is invoked as ``(attempt, exc, delay)`` before each wait so
    callers can log the outage. The last failure is re-raised once the
    attempt budget is exhausted.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    catch = tuple(exceptions)
    delays = backoff_delays(attempts, base=base, factor=factor, cap=cap)
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except catch as exc:
            if attempt == attempts:
                raise
            delay = delays[attempt - 1]
            if on_error is not None:
                on_error(attempt, exc, delay)
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
