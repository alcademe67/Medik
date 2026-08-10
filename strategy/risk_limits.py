"""Portfolio-level risk circuit breakers.

These gate whether a NEW trade may be opened at all, independent of the
per-trade signal/gate/score/sizing checks in signals.py, scoring.py, and
risk.py. A setup that would otherwise be perfect still gets rejected here if
the account has already taken too much damage today/this week/this month, or
if opening it would push position count or exposure past its cap.

Pure and DB-free by design (testable without a journal) -- callers build an
AccountState from live account data plus the equity baselines returned by
strategy.journal.equity_baselines().
"""
from __future__ import annotations

from dataclasses import dataclass

from strategy.config import StrategyConfig


@dataclass(frozen=True)
class AccountState:
    net_liquidation: float
    gross_position_value: float
    open_position_count: int
    equity_start_of_day: float | None = None
    equity_start_of_week: float | None = None
    equity_start_of_month: float | None = None


def _drawdown_pct(current: float, baseline: float) -> float:
    if baseline <= 0:
        return 0.0
    return max(0.0, (baseline - current) / baseline)


def check_trade_allowed(state: AccountState, config: StrategyConfig) -> tuple[bool, str]:
    """Returns (allowed, reason). reason is "" when allowed is True, else a
    human-readable explanation of which limit blocked the trade. Checked in
    order of cheapest-to-explain first; the first breach found is returned."""

    if state.open_position_count >= config.max_concurrent_positions:
        return False, (
            f"max concurrent positions reached "
            f"({state.open_position_count}/{config.max_concurrent_positions})"
        )

    if state.equity_start_of_day is not None:
        dd = _drawdown_pct(state.net_liquidation, state.equity_start_of_day)
        if dd >= config.daily_loss_limit_pct:
            return False, f"daily loss limit hit: -{dd:.1%} (limit {config.daily_loss_limit_pct:.0%})"

    if state.equity_start_of_week is not None:
        dd = _drawdown_pct(state.net_liquidation, state.equity_start_of_week)
        if dd >= config.weekly_drawdown_limit_pct:
            return False, f"weekly drawdown limit hit: -{dd:.1%} (limit {config.weekly_drawdown_limit_pct:.0%})"

    if state.equity_start_of_month is not None:
        dd = _drawdown_pct(state.net_liquidation, state.equity_start_of_month)
        if dd >= config.monthly_drawdown_limit_pct:
            return False, f"monthly drawdown limit hit: -{dd:.1%} (limit {config.monthly_drawdown_limit_pct:.0%})"

    max_exposure = config.max_deployed_pct * state.net_liquidation
    if state.gross_position_value >= max_exposure:
        return False, (
            f"portfolio exposure at cap: ${state.gross_position_value:.2f} >= "
            f"${max_exposure:.2f} ({config.max_deployed_pct:.0%} of equity)"
        )

    return True, ""
