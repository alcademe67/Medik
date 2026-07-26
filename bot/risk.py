"""Risk manager: decides IF a trade may open and HOW BIG it may be.

Every function here is pure (no network, no I/O) so the rules are easy to
test and reason about. The engine calls `check_can_trade` before looking
for entries and `position_size` before submitting one.

Rules enforced (all configurable in bot.config.RiskParams):
  * max risk per trade    — size so that (entry-stop) * size ≈ 1% of equity
  * max position notional — never deploy more than a small % of equity in
                            one position, whatever the stop distance
  * max open positions    — refuse a 4th (default) concurrent position
  * daily loss limit      — halt for the rest of the UTC day past the cap
  * daily order cap       — refuse more than N entry orders in one UTC day
  * consecutive-loss pause — stop after N losing trades in a row
"""

from __future__ import annotations

from dataclasses import dataclass

from bot.config import RISK, RiskParams


@dataclass
class TradeDecision:
    allowed: bool
    reason: str


def check_can_trade(
    *,
    open_positions: int,
    realized_pnl_today: float,
    start_equity_today: float | None,
    consecutive_losses: int,
    orders_today: int = 0,
    params: RiskParams = RISK,
) -> TradeDecision:
    """May the bot open a NEW position right now? First failing rule wins."""
    if open_positions >= params.max_open_positions:
        return TradeDecision(False, f"max open positions ({params.max_open_positions}) reached")

    if params.max_daily_orders > 0 and orders_today >= params.max_daily_orders:
        return TradeDecision(
            False, f"daily order cap ({params.max_daily_orders}) reached"
        )

    if consecutive_losses >= params.max_consecutive_losses:
        return TradeDecision(
            False, f"paused after {consecutive_losses} consecutive losses"
        )

    if start_equity_today and start_equity_today > 0:
        loss_pct = -realized_pnl_today / start_equity_today * 100
        if loss_pct >= params.daily_loss_limit_pct:
            return TradeDecision(
                False, f"daily loss limit hit ({loss_pct:.1f}% ≥ {params.daily_loss_limit_pct}%)"
            )

    return TradeDecision(True, "ok")


def stop_and_target(
    entry_price: float, atr: float, params: RiskParams = RISK
) -> tuple[float, float]:
    """ATR stop-loss and risk/reward take-profit for a long entry."""
    stop = entry_price - params.stop_atr_mult * atr
    risk_per_unit = entry_price - stop
    target = entry_price + params.take_profit_rr * risk_per_unit
    return stop, target


def update_stop(
    *,
    entry_price: float,
    current_stop: float,
    high_water: float,
    price: float,
    atr: float,
    params: RiskParams = RISK,
) -> tuple[float, float]:
    """Return (new_stop, new_high_water) after applying breakeven + ATR trailing.

    Invariant: the stop only ever moves UP (toward locking in profit), never
    down — so a pullback can't loosen your protection. Both features are off
    when their config value is 0.
    """
    high_water = max(high_water, price)
    new_stop = current_stop
    # Breakeven: once price is up breakeven_trigger_pct from entry, protect entry.
    if params.breakeven_trigger_pct > 0 and price >= entry_price * (
        1 + params.breakeven_trigger_pct / 100
    ):
        new_stop = max(new_stop, entry_price)
    # ATR trailing: trail the stop a fixed ATR distance below the high-water mark.
    if params.trailing_atr_mult > 0 and atr > 0:
        new_stop = max(new_stop, high_water - params.trailing_atr_mult * atr)
    return new_stop, high_water


def position_size(
    *,
    equity: float,
    entry_price: float,
    stop_price: float,
    available_quote: float,
    params: RiskParams = RISK,
) -> float:
    """Base-currency size risking at most `max_risk_per_trade` of equity.

    Sized so the loss to the stop equals the risk budget, then capped by
    the cash actually available. Returns 0 if inputs are unusable.
    """
    risk_per_unit = entry_price - stop_price
    if entry_price <= 0 or risk_per_unit <= 0 or equity <= 0:
        return 0.0
    risk_budget = equity * params.max_risk_per_trade
    size = risk_budget / risk_per_unit
    # Cap the position's NOTIONAL to a small % of equity, independent of the
    # stop distance. A tight stop makes the risk sizing above want a huge
    # position; this ceiling keeps any one trade small relative to the account.
    if params.max_position_pct > 0:
        max_notional = equity * params.max_position_pct / 100.0
        size = min(size, max_notional / entry_price)
    # Never spend more cash than we have.
    max_affordable = available_quote / entry_price
    return max(0.0, min(size, max_affordable))
