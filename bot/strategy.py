"""Trading signal logic — pure functions, no network, easy to test and swap.

The default strategy is a moving-average (MA) crossover, the classic
"hello world" of automated trading:

  * the fast average rises above the slow average -> BUY
  * the fast average falls back below the slow    -> SELL
  * otherwise                                     -> HOLD

It is deliberately simple and is NOT a money-maker — it is a correct,
readable skeleton you replace with your own rule. Everything downstream
(order placement, risk caps, dry-run) works the same whatever signal
this returns, so you only ever edit this one file to change strategy.
"""

from enum import Enum


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


def sma(values: list[float], period: int) -> float | None:
    """Simple moving average of the last `period` values, or None if too few."""
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def crossover_signal(
    closes: list[float], fast_period: int, slow_period: int
) -> Signal:
    """Return BUY/SELL/HOLD from a fast/slow SMA crossover.

    `closes` is oldest-to-newest closing prices. We compare this candle's
    fast-vs-slow relationship to the previous candle's, so a signal fires
    only at the *moment* the lines cross — not on every candle where one
    simply happens to sit above the other.
    """
    if len(closes) < slow_period + 1:
        return Signal.HOLD

    fast_now = sma(closes, fast_period)
    slow_now = sma(closes, slow_period)
    fast_prev = sma(closes[:-1], fast_period)
    slow_prev = sma(closes[:-1], slow_period)
    if None in (fast_now, slow_now, fast_prev, slow_prev):
        return Signal.HOLD

    crossed_up = fast_prev <= slow_prev and fast_now > slow_now
    crossed_down = fast_prev >= slow_prev and fast_now < slow_now
    if crossed_up:
        return Signal.BUY
    if crossed_down:
        return Signal.SELL
    return Signal.HOLD
