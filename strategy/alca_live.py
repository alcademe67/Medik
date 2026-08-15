"""Hard live-trading limits for the ALCA multi-timeframe strategy.

These sit BETWEEN a valid signal and an order. `strategy.alca_mtf` answers
"is this a setup?"; this module answers "are we allowed to take it right
now?" -- trade count, damage taken today and this week, and whether we
already hold the symbol.

Pure and clock-free: the caller supplies the session state. Same design as
strategy/risk_limits.py, which covers the portfolio-level breakers; this one
covers the per-session limits the strategy config specifies, which are
tighter (1.5% daily / 3.0% weekly against that module's 3% / 6%).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LiveLimits:
    max_trades_per_day: int = 4
    max_daily_loss_pct: float = 1.5
    max_weekly_loss_pct: float = 3.0
    allow_averaging: bool = False
    max_position_per_symbol: int = 1
    allow_short: bool = True          # INTENT. See resolve_allow_short.


DEFAULT_LIMITS = LiveLimits()


@dataclass
class SessionState:
    equity: float
    equity_start_of_day: float
    equity_start_of_week: float
    trades_today: int = 0
    open_symbols: dict = field(default_factory=dict)   # symbol -> position count


def _loss_pct(current: float, baseline: float) -> float:
    """Percentage drawdown from a baseline; 0.0 if flat or up."""
    if baseline <= 0:
        return 0.0
    return max(0.0, (baseline - current) / baseline * 100.0)


def resolve_allow_short(limits: LiveLimits, account_can_short: bool) -> tuple[bool, str]:
    """Reconcile the CONFIG's intent with what the ACCOUNT can actually do.

    ALLOW_SHORT in the strategy config is an intent, not a capability. The
    account this repo trades is a TFSA, where short selling is prohibited by
    CRA rules for registered accounts. Critically, a SELL order there does
    not open a short -- it sells whatever is held. So a config flag alone
    must never be able to emit SELL signals; the account has to agree.

    Returns (effective, explanation) so a caller can log WHY shorts are off
    rather than silently dropping half the strategy.
    """
    if not limits.allow_short:
        return False, "shorting disabled in config"
    if not account_can_short:
        return False, (
            "config requests shorting but the connected account cannot short "
            "(registered/TFSA accounts are cash-only long); SELL signals "
            "suppressed — in this account a SELL would liquidate a holding, "
            "not open a short"
        )
    return True, "shorting enabled: config allows it and the account supports it"


def check_new_trade(
    symbol: str,
    state: SessionState,
    limits: LiveLimits = DEFAULT_LIMITS,
) -> tuple[bool, str]:
    """May a new position be opened in `symbol` right now?

    Returns (allowed, reason). Checks run cheapest-first and the first breach
    wins, so the reason names the binding limit rather than listing all.
    """
    held = state.open_symbols.get(symbol, 0)
    if held >= limits.max_position_per_symbol:
        return False, (
            f"already holding {held} position(s) in {symbol} "
            f"(max {limits.max_position_per_symbol} per symbol)"
        )
    if held > 0 and not limits.allow_averaging:
        return False, f"averaging into {symbol} is disabled"

    if state.trades_today >= limits.max_trades_per_day:
        return False, (
            f"daily trade cap reached ({state.trades_today}/{limits.max_trades_per_day})"
        )

    daily = _loss_pct(state.equity, state.equity_start_of_day)
    if daily >= limits.max_daily_loss_pct:
        return False, (
            f"daily loss limit hit: -{daily:.2f}% "
            f"(limit {limits.max_daily_loss_pct:.2f}%) — no new trades today"
        )

    weekly = _loss_pct(state.equity, state.equity_start_of_week)
    if weekly >= limits.max_weekly_loss_pct:
        return False, (
            f"weekly loss limit hit: -{weekly:.2f}% "
            f"(limit {limits.max_weekly_loss_pct:.2f}%) — no new trades this week"
        )

    return True, ""


def register_fill(state: SessionState, symbol: str) -> None:
    """Record an opened position. Call this only on a CONFIRMED fill — an
    order that was submitted but never filled must not consume the daily
    trade budget."""
    state.trades_today += 1
    state.open_symbols[symbol] = state.open_symbols.get(symbol, 0) + 1


def register_close(state: SessionState, symbol: str) -> None:
    """Record a position closing. Does NOT decrement trades_today: the cap
    limits how many trades are TAKEN per day, not how many are held at once,
    so closing one does not buy back the right to open another."""
    if state.open_symbols.get(symbol, 0) > 0:
        state.open_symbols[symbol] -= 1
        if state.open_symbols[symbol] == 0:
            del state.open_symbols[symbol]
