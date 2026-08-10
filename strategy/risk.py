"""Position sizing and risk/reward enforcement. This is the code that
actually enforces the "no more than 20% of funds, minimum 1:3 R:R" rule --
strategy/config.py just holds the numbers."""
from __future__ import annotations

import math
from dataclasses import dataclass

from strategy.config import StrategyConfig
from strategy.core_holdings import assert_core_etf


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
class CoreHoldingPlan:
    """A long-term index-ETF holding. Deliberately has no stop, target, or
    risk_reward -- see size_core_holding for why those fields would be lies."""
    symbol: str
    entry: float
    quantity: float
    position_value: float
    pct_of_available_funds: float
    pct_of_equity: float
    binding_cap: str  # "etf_notional" or "portfolio_headroom"


def size_core_holding(
    symbol: str,
    price: float,
    available_funds: float,
    net_liquidation: float,
    gross_position_value: float,
    config: StrategyConfig,
    fractional: bool = True,
) -> CoreHoldingPlan:
    """Size a buy-and-hold position in an approved broad-market index ETF.

    This is a SEPARATE path from size_position, not a flag on it, because the
    swing-trade sizing model does not apply and forcing it to would produce
    numbers that are false:

    * **No stop.** Buy-and-hold has no stop-loss by construction -- the whole
      finding that motivated this strategy is that trading costs and timing
      errors eat the returns. size_position *requires* a stop (it derives the
      target and the position size from the stop distance), so it cannot
      express this position at all.
    * **No target.** There is no take-profit; the horizon is years.
    * **The 1%-of-equity risk rule is therefore inapplicable, not waived.**
      "Capital at risk" in that rule means "dollars lost if the stop fills".
      With no stop that quantity is undefined -- it is not zero, and it is not
      the full position value either. Reporting any number for it would be
      making one up, so CoreHoldingPlan simply has no such field.

    What DOES constrain this position, and is enforced here:

    1. ``config.etf_max_position_pct`` of available funds (70%), permitted only
       because the symbol is a whitelisted diversified index fund.
    2. ``config.max_deployed_pct`` of net liquidation across ALL positions
       (80%), so the cash buffer survives. Whichever binds first wins.

    The drawdown circuit breakers in risk_limits.check_trade_allowed are NOT
    applied here, on purpose. Those exist to stop a swing trader from
    revenge-trading through a losing streak. Applied to a decades-horizon
    index entry they would block buying *during a dip*, which is precisely
    when a long-term holder wants to be buying, and would create a loop where
    a falling market permanently forbids establishing the position.
    """
    sym = assert_core_etf(symbol)

    if price <= 0:
        raise ValueError("price must be positive")
    if available_funds <= 0:
        raise RiskRejected("no available funds")
    if net_liquidation <= 0:
        raise ValueError("net_liquidation must be positive")
    if gross_position_value < 0:
        raise ValueError("gross_position_value cannot be negative")

    etf_notional = available_funds * config.etf_max_position_pct
    headroom = portfolio_headroom(net_liquidation, gross_position_value, config)

    if headroom <= 0:
        raise RiskRejected(
            f"portfolio already at its {config.max_deployed_pct:.0%} deployed cap: "
            f"${gross_position_value:.2f} of ${net_liquidation:.2f} equity is invested, "
            f"leaving no headroom for {sym}"
        )

    max_notional = min(etf_notional, headroom)
    binding_cap = "etf_notional" if etf_notional <= headroom else "portfolio_headroom"

    quantity = _floor_qty(max_notional / price, fractional)

    min_qty = 1e-6 if fractional else 1.0
    if quantity < min_qty:
        raise RiskRejected(
            f"position too small: ${max_notional:.2f} available for {sym} "
            f"can't buy {'a fractional share' if fractional else 'even 1 share'} "
            f"at ${price:.2f} (binding cap: {binding_cap})"
        )

    position_value = quantity * price
    return CoreHoldingPlan(
        symbol=sym,
        entry=price,
        quantity=quantity,
        position_value=position_value,
        pct_of_available_funds=position_value / available_funds,
        pct_of_equity=position_value / net_liquidation,
        binding_cap=binding_cap,
    )


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
    target: float | None = None,
    min_rr: float | None = None,
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
    small account, a strict risk cap combined with a several-dollar stop
    distance can round to 0 whole shares even though a fractional position
    is well within both caps.

    target: optional explicit take-profit (e.g. the pullback strategy's
    swing-high resistance level). When omitted, the target is derived as
    min_rr x the stop distance, exactly as before. When given, it is
    validated against min_rr and rejected if the reward is too small.
    min_rr defaults to config.min_risk_reward; the pullback strategy passes
    config.pullback_min_rr instead.
    """
    if entry <= 0 or stop <= 0:
        raise ValueError("entry and stop must be positive")
    if available_funds <= 0:
        raise RiskRejected("no available funds")

    per_share_risk = abs(entry - stop)
    if per_share_risk <= 0:
        raise RiskRejected("stop is equal to entry - zero risk distance")

    required_rr = min_rr if min_rr is not None else config.min_risk_reward

    if side == "BUY":
        if stop >= entry:
            raise RiskRejected("BUY stop must be below entry")
        if target is None:
            target = entry + required_rr * per_share_risk
        elif (target - entry) < required_rr * per_share_risk - 1e-9:
            raise RiskRejected(
                f"target {target:.2f} is only {(target - entry) / per_share_risk:.1f}R away "
                f"(need {required_rr:.1f}R)"
            )
    elif side == "SELL":
        if stop <= entry:
            raise RiskRejected("SELL (short) stop must be above entry")
        if target is None:
            target = entry - required_rr * per_share_risk
        elif (entry - target) < required_rr * per_share_risk - 1e-9:
            raise RiskRejected(
                f"target {target:.2f} is only {(entry - target) / per_share_risk:.1f}R away "
                f"(need {required_rr:.1f}R)"
            )
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
    risk_reward = abs(target - entry) / per_share_risk  # actual realized R:R for this target

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
