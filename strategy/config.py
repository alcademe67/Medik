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
    max_deployed_pct: float = 0.80  # total invested across ALL positions; >=20% of equity stays cash
    min_risk_reward: float = 3.0  # reject any setup with reward:risk below 1:3

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
