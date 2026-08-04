"""Position sizing and risk/reward enforcement. This is the code that
actually enforces the "no more than 20% of funds, minimum 1:3 R:R" rule --
strategy/config.py just holds the numbers."""
from __future__ import annotations

import math
from dataclasses import dataclass

from strategy.config import StrategyConfig


class RiskRejected(Exception):
    """Raised when a candidate trade fails a hard risk rule."""


def _floor_qty(raw: float, fractional: bool) -> float:
    """Floor a raw share count to a size actually order-able: whole shares
    unless fractional is True, in which case floor to 6 decimal places
    (IBKR's fractional-share increment) so the cap is never exceeded by a
    rounding artifact."""
    if fractional:
        return math.floor(raw * 1_000_000) / 1_000_000
    return float(int(raw))


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
    quantity: float  # whole shares unless size_position was called with fractional=True
    risk_reward: float
    capital_at_risk: float
    position_value: float


def size_position(
    side: str,
    entry: float,
    stop: float,
    available_funds: float,
    config: StrategyConfig,
    net_liquidation: float | None = None,
    fractional: bool = False,
) -> PositionPlan:
    """Compute a position that respects the max-funds-per-trade cap, the
    minimum reward:risk ratio, and (when net_liquidation is supplied) the
    max-2%-of-equity-at-risk cap, given an entry and a stop.

    Target is derived from the stop distance and config.min_risk_reward --
    the caller doesn't get to pass a target that doesn't meet the ratio.

    net_liquidation is optional and defaults to None for backward
    compatibility with existing callers; when omitted, sizing is governed
    only by the notional (max_position_pct) cap, same as before this cap
    was added. Pass net_liquidation to enforce the tighter of the two caps,
    which is the intended behavior for the live pipeline.

    fractional defaults to False, which preserves the original whole-share
    behavior (and existing backtest results, which rely on it). Pass
    fractional=True for accounts that support fractional shares -- on a
    small account, a strict 2%-of-equity risk cap combined with a
    several-dollar stop distance can round to 0 whole shares even though a
    fractional position is well within both caps.
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
    notional_qty = _floor_qty(max_notional / entry, fractional)

    if net_liquidation is not None:
        max_risk_dollars = net_liquidation * config.max_risk_pct
        risk_qty = _floor_qty(max_risk_dollars / per_share_risk, fractional)
        quantity = min(notional_qty, risk_qty)
        binding_cap = "risk" if risk_qty < notional_qty else "notional"
    else:
        quantity = notional_qty
        binding_cap = "notional"

    min_qty = 1e-6 if fractional else 1.0
    if quantity < min_qty:
        raise RiskRejected(
            f"position too small: {config.max_position_pct:.0%} of ${available_funds:.2f} funds "
            + (
                f"and {config.max_risk_pct:.0%} of ${net_liquidation:.2f} equity at ${per_share_risk:.2f}/share risk "
                if net_liquidation is not None
                else ""
            )
            + f"can't buy even 1 share at ${entry:.2f} (binding cap: {binding_cap})"
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
