"""MEDIK SWING — the owner's 1-5 day ETF rotation style, mechanized.

Owner decision, 2026-08-27 (rule 2a in CLAUDE.md): hold a single ETF for
days, take a modest profit, rotate into the next one. Codified exactly as
stated, so the backtest measures the style the owner described rather than
a strategist's improvement of it:

  ENTRY   daily pullback-reclaim structure (strategy/pullback.py checks:
          close above a RISING 200-SMA, pullback touched the 8-EMA within
          the last 5 bars, the pullback low held above the SMA, close
          reclaimed the EMA, today's low is a higher low) — accepted when
          the prior 20-day swing high pays >= MIN_RR against the fixed
          -2.5% stop.
  SIZE    one position at a time, DEPLOY_FRACTION of cash, fractional
          shares (the account is ~$300; whole shares of SPY don't exist
          at that size).
  EXITS   target = the swing high (real resistance, a limit order),
          stop = entry * (1 - STOP_PCT) (a stop order),
          time  = close of the MAX_HOLD_SESSIONS-th session after entry.
          Whichever comes first. No trailing, no averaging down.

Everything here is pure computation on completed daily bars — no broker,
no clock, no orders. The backtest and any live wiring import THESE
functions so the thing measured is the thing that would trade.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from strategy.config import StrategyConfig
from strategy.pullback import compute_pullback_frame, evaluate_pullback

STOP_PCT = 0.025            # owner: fixed -2.5% stop
MIN_RR = 1.0                # owner: accept 1:1 against that stop
MAX_HOLD_SESSIONS = 5       # owner: "three days, four days, five days watch"
DEPLOY_FRACTION = 0.90      # owner: one position, ~all-in, keep 10% back
SWING_UNIVERSE = [
    "TQQQ", "SOXL", "SNXX",          # leveraged momentum
    "QQQ", "SPY", "IWM",             # broad market
    "SMH", "XLK", "XLF", "XLE",      # liquid sectors
    "DIA", "VTI",                    # additional broad funds
]

# evaluate_pullback re-checks its own structural R:R against the pullback
# low; rule 2a replaces that stop, so the structural gate is disabled and
# the decision is remade below against the owner's stop.
_LOOSE = StrategyConfig(pullback_min_rr=0.01)


@dataclass(frozen=True)
class SwingSignal:
    passed: bool
    entry: float = 0.0          # reference price (the signal bar's close)
    stop: float = 0.0           # entry * (1 - STOP_PCT)
    target: float = 0.0         # prior swing high
    reward_risk: float = 0.0
    reason: str = ""


def evaluate_swing(df: pd.DataFrame) -> SwingSignal:
    """Evaluate the LAST completed daily bar of one symbol.

    `df` needs open/high/low/close/volume columns, oldest first, and at
    least ~205 completed rows (the 200-SMA must exist).
    """
    sig = evaluate_pullback(compute_pullback_frame(df), _LOOSE)
    if not sig.passed:
        return SwingSignal(False, reason=sig.reason)

    entry = float(sig.entry)
    target = float(sig.target)
    stop = entry * (1 - STOP_PCT)
    risk = entry - stop
    if risk <= 0:
        return SwingSignal(False, reason="degenerate stop")
    rr = (target - entry) / risk
    if rr < MIN_RR:
        return SwingSignal(False, entry, stop, target, rr,
                           f"target {target:.2f} pays only {rr:.2f}R "
                           f"against the -{STOP_PCT:.1%} stop")
    return SwingSignal(True, entry, stop, target, rr,
                       f"pullback-reclaim, {rr:.2f}R to swing high")


def size_swing(cash: float, fill: float) -> float:
    """Fractional share quantity for one position: DEPLOY_FRACTION of cash.

    Returns 0.0 when cash cannot buy a meaningful sliver (guards the
    degenerate case where commissions exceed the position)."""
    if fill <= 0 or cash <= 0:
        return 0.0
    qty = (cash * DEPLOY_FRACTION) / fill
    return qty if qty * fill >= 25.0 else 0.0


def swing_exit(bar_open: float, bar_high: float, bar_low: float,
               bar_close: float, stop: float, target: float,
               sessions_held: int) -> tuple[bool, float, str]:
    """Exit decision for ONE daily bar of an open swing position.

    Order of evaluation mirrors the intraday backtester's pessimism:
    a gap through the stop fills at the OPEN, the stop is assumed BEFORE
    the target when one bar spans both (intrabar order is unknowable),
    and the limit target fills exactly at the target with no improvement.
    The time exit fires at the close of the last allowed session.
    """
    if bar_open <= stop:
        return True, bar_open, "gap through stop"
    if bar_low <= stop:
        return True, stop, "stop"
    if bar_high >= target:
        return True, target, "target"
    if sessions_held >= MAX_HOLD_SESSIONS:
        return True, bar_close, "time exit"
    return False, 0.0, ""
