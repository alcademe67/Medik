"""Medik multi-timeframe strategy — weekly regime, daily trend, 15m entry.

A pure-Python port of the pasted `strategy.py`, with the defects found in
review fixed. No pandas: the rest of this repo is pure Python, pandas is not
installed in every environment it runs in, and the list-based form is
directly testable.

FIXES APPLIED (each has a regression test in tests/test_medik_mtf.py)
--------------------------------------------------------------------
1. **RSI no longer returns NaN in a clean uptrend.** The original computed
   ``rs = avg_gain / avg_loss.replace(0, nan)``. In a strictly rising series
   ``avg_loss`` reaches exactly 0, so RSI became NaN, every comparison
   against it was False, and the weekly regime read NEUTRAL -- suppressing
   long signals in precisely the regime the strategy exists to trade. RSI is
   now 100 when there is no average loss.

2. **Confidence is graded, so MIN_CONFIDENCE actually filters.** The original
   added six constants inside the all-conditions-passed branch, summing to
   exactly 1.00 every time; ``confidence >= 0.75`` could never reject
   anything. Confidence is now built from continuous sub-scores (where RSI
   sits in its band, how far price is stretched from the EMA, volume
   strength, timeframe alignment), so it varies between setups and the
   threshold does real work.

3. **Only completed bars are evaluated.** The original read ``iloc[-1]``,
   which intraday is the *forming* bar -- signals appear and vanish as it
   ticks. ``drop_forming_bar`` is provided and the backtest uses it. This is
   the same failure that produced a false gate result on 2026-08-03 and led
   to strategy/data_quality.py.

4. **The liquidity filter uses average daily volume**, not a single 15-minute
   bar's volume. 100,000 shares is a trivial daily threshold and a severe
   15-minute one; applying it per-bar silently excluded most of the S&P 500
   outside the opening hour.

5. **Shorting is gated on the account, not a module constant.** The original
   had ``ALLOW_SHORT = True`` at module scope. This account is a TFSA: it
   cannot short, and a SELL on a symbol it holds does not open a short -- it
   liquidates the holding. ``account_can_short`` defaults False and must be
   passed explicitly by a caller that has checked the account.

Also: ATR here uses Wilder's smoothing rather than a simple rolling mean, so
stop distances match what charting software shows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# ---------------------------------------------------------------- config

WEEKLY_FAST_EMA, WEEKLY_SLOW_EMA, WEEKLY_RSI = 10, 30, 14
DAILY_FAST_EMA, DAILY_SLOW_EMA, DAILY_RSI = 9, 21, 14
ENTRY_FAST_EMA, ENTRY_SLOW_EMA, ENTRY_RSI, ENTRY_ATR = 9, 21, 14, 14

LONG_RSI_MIN, LONG_RSI_MAX = 52.0, 68.0
SHORT_RSI_MIN, SHORT_RSI_MAX = 32.0, 48.0

ATR_STOP_MULTIPLIER = 1.5
RISK_REWARD = 2.0
MAX_DISTANCE_FROM_EMA = 0.025
MIN_CONFIDENCE = 0.75

MIN_PRICE = 5.00
MIN_AVG_DAILY_VOLUME = 100_000

# Volume: the current entry bar against its own trailing average. This is a
# HARD gate, not just a confidence input -- a breakout on below-average
# volume is the classic false signal.
VOLUME_LOOKBACK = 20
MIN_VOLUME_RATIO = 1.0

# Closed-candle discipline. See drop_forming_bar: an in-progress bar makes
# signals appear and vanish as it ticks.
USE_CLOSED_WEEKLY = True
USE_CLOSED_DAILY = True
USE_CLOSED_ENTRY = True


def volume_ratio_of(bars: Sequence[OHLCV], lookback: int = VOLUME_LOOKBACK) -> float:
    """Current bar's volume divided by the average of the `lookback` bars
    BEFORE it. The current bar is excluded from its own average — including
    it drags the baseline toward the value being tested."""
    if len(bars) < lookback + 1:
        return 1.0
    window = bars[-(lookback + 1):-1]
    avg = sum(b.volume for b in window) / len(window)
    if avg <= 0:
        return 1.0
    return bars[-1].volume / avg


@dataclass(frozen=True)
class OHLCV:
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Signal:
    action: str                     # "BUY" | "SELL" | "HOLD"
    symbol: str
    price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    confidence: float = 0.0
    reason: str = ""
    weekly_trend: str = "NEUTRAL"
    daily_trend: str = "NEUTRAL"


# ------------------------------------------------------------ indicators


def ema(values: Sequence[float], span: int) -> list[float]:
    """Exponential moving average, adjust=False — matches pandas' ewm."""
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(out[-1] + alpha * (v - out[-1]))
    return out


def rsi(closes: Sequence[float], period: int = 14) -> list[float]:
    """Wilder's RSI.

    FIX 1: when average loss is zero the original produced NaN. There is a
    correct answer -- no losses means maximum strength, RSI 100 -- and NaN
    silently disabled every downstream comparison.
    """
    if len(closes) < 2:
        return [50.0] * len(closes)
    alpha = 1.0 / period
    avg_gain = avg_loss = 0.0
    out = [50.0]
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        avg_gain += alpha * (max(delta, 0.0) - avg_gain)
        avg_loss += alpha * (max(-delta, 0.0) - avg_loss)
        if avg_loss == 0.0:
            out.append(100.0 if avg_gain > 0.0 else 50.0)
        elif avg_gain == 0.0:
            out.append(0.0)
        else:
            out.append(100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    return out


def atr(bars: Sequence[OHLCV], period: int = 14) -> list[float]:
    """Average True Range with Wilder's smoothing."""
    if not bars:
        return []
    out = [bars[0].high - bars[0].low]
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - prev_close),
            abs(bars[i].low - prev_close),
        )
        out.append(out[-1] + (tr - out[-1]) / period)
    return out


def drop_forming_bar(bars: Sequence[OHLCV], bar_complete: bool) -> list[OHLCV]:
    """FIX 3: discard the in-progress bar so signals cannot flicker."""
    bars = list(bars)
    if not bar_complete and bars:
        return bars[:-1]
    return bars


# --------------------------------------------------------------- regimes


def _trend(bars: Sequence[OHLCV], fast: int, slow: int, rsi_period: int,
           compare_to_slow: bool) -> str:
    """Shared weekly/daily regime logic.

    compare_to_slow mirrors the original: weekly compares close to the FAST
    ema, daily compares it to the SLOW ema.
    """
    closes = [b.close for b in bars]
    if len(closes) < max(slow, rsi_period) + 2:
        return "NEUTRAL"
    ef, es = ema(closes, fast)[-1], ema(closes, slow)[-1]
    r = rsi(closes, rsi_period)[-1]
    close = closes[-1]
    reference = es if compare_to_slow else ef
    if ef > es and close > reference and r > 50.0:
        return "BULLISH"
    if ef < es and close < reference and r < 50.0:
        return "BEARISH"
    return "NEUTRAL"


def weekly_trend(bars: Sequence[OHLCV]) -> str:
    return _trend(bars, WEEKLY_FAST_EMA, WEEKLY_SLOW_EMA, WEEKLY_RSI, compare_to_slow=False)


def daily_trend(bars: Sequence[OHLCV]) -> str:
    return _trend(bars, DAILY_FAST_EMA, DAILY_SLOW_EMA, DAILY_RSI, compare_to_slow=True)


# ------------------------------------------------------------ confidence


def _band_score(value: float, low: float, high: float) -> float:
    """1.0 at the centre of the band, tapering to 0.0 at either edge.

    FIX 2: a graded score. RSI at 60 (mid-band) is a better long than RSI at
    52.1 (about to fall out of the band), and the original could not say so.
    """
    if not low <= value <= high:
        return 0.0
    mid = (low + high) / 2.0
    half = (high - low) / 2.0
    if half <= 0:
        return 0.0
    return max(0.0, 1.0 - abs(value - mid) / half)


def _closeness_score(distance: float, limit: float) -> float:
    """1.0 when price sits on the EMA, 0.0 at the max-distance limit."""
    if limit <= 0 or distance >= limit:
        return 0.0
    return 1.0 - distance / limit


def confidence_score(
    rsi_value: float,
    rsi_low: float,
    rsi_high: float,
    distance_from_ema: float,
    volume_ratio: float,
    weekly_aligned: bool,
    daily_aligned: bool,
) -> float:
    """Weighted, continuous — so MIN_CONFIDENCE rejects marginal setups."""
    return (
        0.25 * (1.0 if weekly_aligned else 0.0)
        + 0.20 * (1.0 if daily_aligned else 0.0)
        + 0.25 * _band_score(rsi_value, rsi_low, rsi_high)
        + 0.15 * _closeness_score(distance_from_ema, MAX_DISTANCE_FROM_EMA)
        + 0.15 * min(1.0, volume_ratio / 2.0)
    )


# ------------------------------------------------------------- the signal


def generate_signal(
    symbol: str,
    weekly: Sequence[OHLCV],
    daily: Sequence[OHLCV],
    entry: Sequence[OHLCV],
    avg_daily_volume: float,
    volume_ratio: float = 1.0,
    account_can_short: bool = False,
) -> Signal:
    """Evaluate one symbol. `entry` must contain COMPLETED bars only.

    account_can_short (FIX 5) must be passed True by a caller that has
    verified the account permits shorting. It is False by default because
    the account this repo trades is a TFSA, where a SELL signal does not
    open a short -- it sells whatever is held.
    """
    if len(entry) < max(ENTRY_SLOW_EMA, ENTRY_RSI, ENTRY_ATR) + 5:
        price = entry[-1].close if entry else 0.0
        return Signal("HOLD", symbol, price, reason="not enough entry bars")

    closes = [b.close for b in entry]
    price = closes[-1]
    prev_close = closes[-2]
    ef = ema(closes, ENTRY_FAST_EMA)[-1]
    es = ema(closes, ENTRY_SLOW_EMA)[-1]
    r = rsi(closes, ENTRY_RSI)[-1]
    a = atr(entry, ENTRY_ATR)[-1]

    wk, dl = weekly_trend(weekly), daily_trend(daily)

    if price < MIN_PRICE:
        return Signal("HOLD", symbol, price, reason=f"price ${price:.2f} below ${MIN_PRICE:.2f}",
                      weekly_trend=wk, daily_trend=dl)
    # FIX 4: average daily volume, not this bar's volume.
    if avg_daily_volume < MIN_AVG_DAILY_VOLUME:
        return Signal("HOLD", symbol, price,
                      reason=f"avg daily volume {avg_daily_volume:,.0f} below {MIN_AVG_DAILY_VOLUME:,}",
                      weekly_trend=wk, daily_trend=dl)
    if a <= 0:
        return Signal("HOLD", symbol, price, reason="invalid ATR", weekly_trend=wk, daily_trend=dl)
    if volume_ratio < MIN_VOLUME_RATIO:
        return Signal("HOLD", symbol, price,
                      reason=f"volume ratio {volume_ratio:.2f} below {MIN_VOLUME_RATIO:.2f}",
                      weekly_trend=wk, daily_trend=dl)

    distance = abs(price - ef) / ef if ef else 1.0

    # ---- long
    if wk == "BULLISH" and dl == "BULLISH":
        if ef > es and price > ef and LONG_RSI_MIN <= r <= LONG_RSI_MAX \
                and distance <= MAX_DISTANCE_FROM_EMA and price > prev_close:
            conf = confidence_score(r, LONG_RSI_MIN, LONG_RSI_MAX, distance, volume_ratio, True, True)
            if conf >= MIN_CONFIDENCE:
                stop = price - a * ATR_STOP_MULTIPLIER
                return Signal("BUY", symbol, price, round(stop, 2),
                              round(price + (price - stop) * RISK_REWARD, 2),
                              round(conf, 4),
                              "weekly+daily bullish, 15m EMA momentum, RSI confirmed", wk, dl)
            return Signal("HOLD", symbol, price, confidence=round(conf, 4),
                          reason=f"confidence {conf:.2f} below {MIN_CONFIDENCE}",
                          weekly_trend=wk, daily_trend=dl)

    # ---- short
    if account_can_short and wk == "BEARISH" and dl == "BEARISH":
        if ef < es and price < ef and SHORT_RSI_MIN <= r <= SHORT_RSI_MAX \
                and distance <= MAX_DISTANCE_FROM_EMA and price < prev_close:
            conf = confidence_score(r, SHORT_RSI_MIN, SHORT_RSI_MAX, distance, volume_ratio, True, True)
            if conf >= MIN_CONFIDENCE:
                stop = price + a * ATR_STOP_MULTIPLIER
                return Signal("SELL", symbol, price, round(stop, 2),
                              round(price - (stop - price) * RISK_REWARD, 2),
                              round(conf, 4),
                              "weekly+daily bearish, 15m EMA momentum, RSI confirmed", wk, dl)

    return Signal("HOLD", symbol, price, confidence=0.0,
                  reason=f"no setup | weekly={wk} | daily={dl}", weekly_trend=wk, daily_trend=dl)
