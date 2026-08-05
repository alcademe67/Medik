"""Durable trading rules. Values here are the account's standing risk policy —
see CLAUDE.md for the plain-language version. Changing these numbers changes
what the strategy is allowed to do; don't edit casually.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyConfig:
    # Position sizing
    max_position_pct: float = 0.20  # never commit more than 20% of available funds to one trade
    etf_max_position_pct: float = 0.70  # raised cap for APPROVED broad-market index ETFs only
    # (owner decision 2026-08-05). The 20% cap limits single-issuer blowup risk,
    # which doesn't apply the same way to a fund holding 100+ companies. Eligibility
    # is by explicit whitelist in strategy/core_holdings.py, NOT by asset class --
    # leveraged/inverse ETFs (TQQQ, SQQQ, ...) are ETFs too and are never eligible.
    max_deployed_pct: float = 0.80  # total invested across ALL positions; >=20% of equity stays cash
    min_risk_reward: float = 3.0  # gate strategy: reject setups below 1:3 (derived target)
    pullback_min_rr: float = 2.0  # pullback strategy: target is the PRIOR SWING HIGH (real
    # resistance, not a derived multiple); skip if that target is closer than 2R.
    # Both R:R styles per the swing methodology adopted 2026-08-04.
    max_risk_pct: float = 0.01  # never risk more than 1% of net liquidation on one trade's stop
    # (tightened from 2% on 2026-08-04, owner-delegated: matches the adopted
    # methodology's guidance for developing accounts; the old 2% remains the
    # absolute ceiling this must never be raised above without the owner.)

    # Portfolio-level circuit breakers
    max_concurrent_positions: int = 5
    daily_loss_limit_pct: float = 0.03  # halt new entries if today's equity drop exceeds this
    weekly_drawdown_limit_pct: float = 0.06
    monthly_drawdown_limit_pct: float = 0.10

    # In-trade management (applied to OPEN positions, not entries)
    breakeven_at_r: float = 1.0  # move stop to entry once price has moved this many R in favor
    trailing_stop_activate_r: float = 1.5  # start trailing once price has moved this many R in favor
    trailing_stop_atr_mult: float = 2.0  # trail distance behind price, in ATR
    partial_profit_r: float = 2.0  # take partial profit once price reaches this many R
    partial_profit_fraction: float = 0.5  # fraction of the position to close at partial_profit_r

    # Trend filter (EMA crossover)
    ema_fast: int = 50
    ema_slow: int = 200

    # Momentum filters
    rsi_period: int = 14
    rsi_buy_max: float = 70.0  # don't chase — reject longs once RSI is overbought
    rsi_buy_min: float = 50.0  # require upside momentum, not just "not oversold"
    rsi_sell_min: float = 30.0
    rsi_sell_max: float = 50.0

    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    # Volume confirmation
    volume_sma_period: int = 20
    volume_min_ratio: float = 1.0  # today's volume must be >= N x its 20-day average

    # Volatility / stop sizing
    atr_period: int = 14
    atr_stop_mult: float = 2.0  # stop = entry -/+ (atr_stop_mult * ATR)

    # Warm-up bars needed before any indicator is valid
    @property
    def warmup_bars(self) -> int:
        return max(self.ema_slow, self.rsi_period, self.macd_slow + self.macd_signal, self.atr_period, self.volume_sma_period) + 1


DEFAULT_CONFIG = StrategyConfig()
