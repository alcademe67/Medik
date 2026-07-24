"""Risk manager: decides IF a trade may open and HOW BIG it may be.

Every function here is pure (no network, no I/O) so the rules are easy to
test and reason about. The engine calls `check_can_trade` before looking
for entries and `position_size` before submitting one.

Rules enforced (all configurable in bot.config.RiskParams):
  * max risk per trade   — size so that (entry-stop) * size ≈ 1% of equity
  * max open positions    — refuse a 4th (default) concurrent position
  * daily loss limit      — halt for the rest of the UTC day past the cap
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
    params: RiskParams = RISK,
) -> TradeDecision:
    """May the bot open a NEW position right now? First failing rule wins."""
    if open_positions >= params.max_open_positions:
        return TradeDecision(False, f"max open positions ({params.max_open_positions}) reached")

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
    # Never spend more cash than we have.
    max_affordable = available_quote / entry_price
    return max(0.0, min(size, max_affordable))
