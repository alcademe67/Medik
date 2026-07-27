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


def size_breakdown(
    *,
    equity: float,
    entry_price: float,
    stop_price: float,
    available_quote: float,
    params: RiskParams = RISK,
) -> dict:
    """Compute the position size AND every intermediate term + a plain-English
    reason, so a caller can log exactly why the size is what it is (including
    why it is zero). `position_size` is the thin wrapper that returns just the
    final size; this is the same math with the work shown.

    Terms:
      * risk_size      — units so the loss to the stop == max_risk_per_trade
      * cap_size       — units allowed by the max_position_pct notional ceiling
      * affordable     — units the available quote balance can actually buy
      * size           — max(0, min(risk_size, cap_size, affordable))
    """
    risk_per_unit = entry_price - stop_price
    info: dict = {
        "equity": equity,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "risk_per_unit": risk_per_unit,
        "available_quote": available_quote,
        "risk_budget": 0.0,
        "risk_size": 0.0,
        "cap_size": float("inf"),
        "affordable": 0.0,
        "size": 0.0,
        "reason": "",
    }
    if entry_price <= 0:
        info["reason"] = f"entry_price {entry_price} <= 0 (no valid price)"
        return info
    if risk_per_unit <= 0:
        info["reason"] = (
            f"risk_per_unit {risk_per_unit} <= 0 — stop {stop_price} is not below "
            f"entry {entry_price} (ATR likely 0)"
        )
        return info
    if equity <= 0:
        info["reason"] = f"equity {equity} <= 0 (no account value to size against)"
        return info

    risk_budget = equity * params.max_risk_per_trade
    risk_size = risk_budget / risk_per_unit
    info["risk_budget"] = risk_budget
    info["risk_size"] = risk_size
    size = risk_size
    binding = "risk-per-trade budget"

    if params.max_position_pct > 0:
        cap_size = (equity * params.max_position_pct / 100.0) / entry_price
        info["cap_size"] = cap_size
        if cap_size < size:
            size, binding = cap_size, f"max_position_pct cap ({params.max_position_pct}%)"

    # Reserve a slice of cash for the taker fee + slippage so a market order's
    # cost can't exceed the balance (KuCoin 200004). Never size to 100% of funds.
    usable_quote = available_quote * (1.0 - params.cash_buffer)
    affordable = max(0.0, usable_quote) / entry_price
    info["affordable"] = affordable
    if affordable < size:
        size, binding = affordable, f"available cash (≤{(1 - params.cash_buffer) * 100:.0f}% of balance)"

    size = max(0.0, size)
    info["size"] = size
    if size <= 0:
        info["reason"] = (
            f"available_quote {available_quote} buys 0 units at {entry_price}"
            if available_quote <= 0 else "computed size collapsed to 0"
        )
    else:
        info["reason"] = f"binding constraint = {binding}"
    return info


def position_size(
    *,
    equity: float,
    entry_price: float,
    stop_price: float,
    available_quote: float,
    params: RiskParams = RISK,
) -> float:
    """Base-currency size risking at most `max_risk_per_trade` of equity, then
    capped by the notional ceiling and the cash on hand. 0 if inputs are
    unusable. See `size_breakdown` for the itemised math and the reason."""
    return size_breakdown(
        equity=equity, entry_price=entry_price, stop_price=stop_price,
        available_quote=available_quote, params=params,
    )["size"]
