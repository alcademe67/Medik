"""In-trade risk management: breakeven stops, trailing stops, partial
profit-taking -- for POSITIONS ALREADY OPEN, as opposed to strategy.risk
which sizes NEW entries.

Every function here is a pure decision: given the current position and
price, what should the stop/exit look like now? None of them touch an
order. Applying the result (moving a stop, selling part of a position)
still goes through the same draft-and-tap path as every other order in
this repo -- see ibkr/orders.py and examples/protect_positions.py for the
confirm=True pattern these decisions feed into.
"""
from __future__ import annotations

from dataclasses import dataclass

from strategy.config import StrategyConfig, DEFAULT_CONFIG


@dataclass(frozen=True)
class OpenPosition:
    side: str  # "BUY" or "SELL"
    entry: float
    stop: float  # current resting stop
    quantity: float
    atr_at_entry: float  # ATR at the time of entry, for the original stop distance


def _initial_risk_per_share(pos: OpenPosition) -> float:
    return abs(pos.entry - pos.stop)


def r_multiple(pos: OpenPosition, current_price: float) -> float:
    """How many multiples of the original risk the position has moved in
    the trade's favor (negative if it's moved against)."""
    risk = _initial_risk_per_share(pos)
    if risk <= 0:
        return 0.0
    move = (current_price - pos.entry) if pos.side == "BUY" else (pos.entry - current_price)
    return move / risk


def breakeven_stop(pos: OpenPosition, current_price: float, config: StrategyConfig = DEFAULT_CONFIG) -> float | None:
    """Once the position has moved config.breakeven_at_r in its favor,
    the stop should move to entry (locking in a scratch-or-better trade).
    Returns the new stop price, or None if not yet triggered or if the
    current stop is already at/past breakeven (never loosens a stop)."""
    if r_multiple(pos, current_price) < config.breakeven_at_r:
        return None
    if pos.side == "BUY" and pos.stop >= pos.entry:
        return None
    if pos.side == "SELL" and pos.stop <= pos.entry:
        return None
    return pos.entry


def trailing_stop(
    pos: OpenPosition,
    current_price: float,
    current_atr: float,
    config: StrategyConfig = DEFAULT_CONFIG,
) -> float | None:
    """Once the position has moved config.trailing_stop_activate_r in its
    favor, trail the stop config.trailing_stop_atr_mult * ATR behind the
    current price. Only ever tightens the stop (moves it in the position's
    favor) -- never returns a level that would loosen it. Returns None if
    trailing hasn't activated yet or the computed level wouldn't improve
    the current stop."""
    if r_multiple(pos, current_price) < config.trailing_stop_activate_r:
        return None

    trail_distance = config.trailing_stop_atr_mult * current_atr
    if pos.side == "BUY":
        candidate = current_price - trail_distance
        if candidate <= pos.stop:
            return None
        return candidate
    else:
        candidate = current_price + trail_distance
        if candidate >= pos.stop:
            return None
        return candidate


@dataclass(frozen=True)
class PartialProfitPlan:
    sell_quantity: float
    remaining_quantity: float
    trigger_price: float


def partial_profit_plan(
    pos: OpenPosition,
    current_price: float,
    config: StrategyConfig = DEFAULT_CONFIG,
) -> PartialProfitPlan | None:
    """Once price reaches config.partial_profit_r, close
    config.partial_profit_fraction of the position and let the rest run
    (typically with the stop already moved to breakeven per breakeven_stop).
    Returns None if not yet triggered."""
    if r_multiple(pos, current_price) < config.partial_profit_r:
        return None

    risk = _initial_risk_per_share(pos)
    trigger_price = (
        pos.entry + config.partial_profit_r * risk
        if pos.side == "BUY"
        else pos.entry - config.partial_profit_r * risk
    )
    sell_qty = pos.quantity * config.partial_profit_fraction
    return PartialProfitPlan(
        sell_quantity=sell_qty,
        remaining_quantity=pos.quantity - sell_qty,
        trigger_price=trigger_price,
    )
