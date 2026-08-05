"""Trend-pullback swing strategy (long-only).

Modeled on the swing-trading system taught publicly by Humbled Trader
(Shay Huang, humbledtrader.com): trade WITH a strong uptrend, but enter on
weakness -- a controlled pullback to the short-term EMA -- rather than
chasing strength. Stop goes under the pullback low (structure, not a
volatility multiple), so risk stays tight relative to the trend's upside.

Rules implemented here (daily bars):

  Trend filter:   close > 200 SMA, and the 200 SMA itself rising.
  Setup:          price pulled back to/below the 8 EMA within the last
                  few bars (touched or pierced it) after a recent swing
                  high, while holding above the 200 SMA.
  Trigger:        today's bar closes back above the 8 EMA with a higher
                  low than the pullback low -- i.e. the pullback has
                  rejected and the trend is resuming.
  Stop:           just below the lowest low of the pullback.
  Target:         the recent swing high -- actual resistance, where the
                  methodology says to take profit -- NOT a derived
                  multiple. If that target is closer than
                  config.pullback_min_rr x the stop distance (1:2), the
                  trade isn't worth taking and the signal does not fire.

Deliberately different from strategy/signals.py: there is NO same-day
volume-surge requirement. Healthy pullbacks print LOW volume by nature --
demanding above-average volume on the signal bar filters out exactly the
entries this strategy exists to take. Volume sanity lives in universe
selection (liquid names only), not the trigger.

Long-only by design: the pullback-short mirror is intentionally not
implemented (the owner's TFSA cannot short).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from strategy.config import StrategyConfig, DEFAULT_CONFIG

TREND_SMA = 200
PULLBACK_EMA = 8
PULLBACK_LOOKBACK = 5   # bars to scan for the EMA touch + pullback low
SWING_HIGH_LOOKBACK = 20  # a recent swing high must exist above current price


@dataclass(frozen=True)
class PullbackSignal:
    passed: bool
    entry: float | None = None
    stop: float | None = None
    target: float | None = None  # the swing high; realized R:R = (target-entry)/(entry-stop)
    reason: str = ""
    checks: dict = field(default_factory=dict)


def compute_pullback_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["sma_trend"] = out["close"].rolling(TREND_SMA).mean()
    out["ema_pullback"] = out["close"].ewm(span=PULLBACK_EMA, adjust=False).mean()
    return out


def evaluate_pullback(df: pd.DataFrame, config: StrategyConfig = DEFAULT_CONFIG) -> PullbackSignal:
    """Evaluate the LAST row of a frame produced by compute_pullback_frame.
    Needs at least TREND_SMA + PULLBACK_LOOKBACK completed bars."""
    if len(df) < TREND_SMA + PULLBACK_LOOKBACK + 1:
        return PullbackSignal(False, reason="insufficient history")

    row = df.iloc[-1]
    if pd.isna(row["sma_trend"]) or pd.isna(row["ema_pullback"]):
        return PullbackSignal(False, reason="indicators not yet valid")

    window = df.iloc[-(PULLBACK_LOOKBACK + 1):-1]  # the pullback bars before today
    recent = df.iloc[-(SWING_HIGH_LOOKBACK + 1):-1]

    close = float(row["close"])
    sma_now = float(row["sma_trend"])
    sma_then = float(df["sma_trend"].iloc[-6])
    ema_now = float(row["ema_pullback"])

    pullback_low = float(window["low"].min())
    touched_ema = bool((window["low"] <= window["ema_pullback"]).any())
    swing_high = float(recent["high"].max())

    checks = {
        "trend_above_sma200": close > sma_now,
        "sma200_rising": sma_now > sma_then,
        "recent_swing_high_above": swing_high > close,
        "pulled_back_to_ema": touched_ema,
        "pullback_held_above_sma": pullback_low > sma_now,
        "reclaimed_ema": close > ema_now,
        "higher_low_vs_pullback": float(row["low"]) > pullback_low,
    }

    if not all(checks.values()):
        failed = [k for k, v in checks.items() if not v]
        return PullbackSignal(False, reason=f"failed: {failed}", checks=checks)

    stop = round(pullback_low * 0.995, 4)  # a hair under the pullback low
    if stop >= close:
        return PullbackSignal(False, reason="stop >= entry (degenerate pullback)", checks=checks)

    # Target = the swing high (real resistance). Require at least
    # pullback_min_rr reward per unit risk to that target, else skip.
    risk = close - stop
    reward = swing_high - close
    if reward < config.pullback_min_rr * risk:
        return PullbackSignal(
            False,
            reason=f"target too close: swing high {swing_high:.2f} is "
                   f"{reward / risk:.1f}R away (need {config.pullback_min_rr:.0f}R)",
            checks=checks,
        )

    return PullbackSignal(True, entry=close, stop=stop, target=round(swing_high, 4),
                           reason="pullback resumption", checks=checks)
