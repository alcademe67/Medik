"""Position sizing and risk/reward enforcement. This is the code that
actually enforces the "no more than 20% of funds, minimum 1:3 R:R" rule --
strategy/config.py just holds the numbers."""
from __future__ import annotations

from dataclasses import dataclass

from strategy.config import StrategyConfig


class RiskRejected(Exception):
    """Raised when a candidate trade fails a hard risk rule."""


def portfolio_headroom(net_liquidation: float, gross_position_value: float, config: StrategyConfig) -> float:
    """Capital still deployable before the account hits its max-invested
    ceiling (e.g. 80% of equity). New entries must fit inside this."""
    return max(0.0, config.max_deployed_pct * net_liquidation - gross_position_value)


@dataclass(frozen=True)
class PositionPlan:
    side: str  # "BUY" or "SELL" (SELL = short entry)
    entry: float
    stop: float
    target: float
    quantity: int
    risk_reward: float
    capital_at_risk: float
    position_value: float


def size_position(
    side: str,
    entry: float,
    stop: float,
    available_funds: float,
    config: StrategyConfig,
) -> PositionPlan:
    """Compute a position that respects both the max-funds-per-trade cap and
    the minimum reward:risk ratio, given an entry and a stop.

    Target is derived from the stop distance and config.min_risk_reward --
    the caller doesn't get to pass a target that doesn't meet the ratio.
    """
    if entry <= 0 or stop <= 0:
        raise ValueError("entry and stop must be positive")
    if available_funds <= 0:
        raise RiskRejected("no available funds")

    per_share_risk = abs(entry - stop)
    if per_share_risk <= 0:
        raise RiskRejected("stop is equal to entry - zero risk distance")

    if side == "BUY":
        if stop >= entry:
            raise RiskRejected("BUY stop must be below entry")
        target = entry + config.min_risk_reward * per_share_risk
    elif side == "SELL":
        if stop <= entry:
            raise RiskRejected("SELL (short) stop must be above entry")
        target = entry - config.min_risk_reward * per_share_risk
    else:
        raise ValueError("side must be BUY or SELL")

    max_notional = available_funds * config.max_position_pct
    quantity = int(max_notional // entry)
    if quantity < 1:
        raise RiskRejected(
            f"available funds too small: {config.max_position_pct:.0%} of "
            f"${available_funds:.2f} can't buy even 1 share at ${entry:.2f}"
        )

    position_value = quantity * entry
    capital_at_risk = quantity * per_share_risk
    risk_reward = config.min_risk_reward  # target is derived to hit this exactly

    return PositionPlan(
        side=side,
        entry=entry,
        stop=stop,
        target=target,
        quantity=quantity,
        risk_reward=risk_reward,
        capital_at_risk=capital_at_risk,
        position_value=position_value,
    )
