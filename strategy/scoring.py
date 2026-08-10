"""Ranks gate-passing candidates 0-100 so the best setups can be prioritized
over merely-qualifying ones.

This sits ON TOP of the hard 4-indicator gate in strategy/signals.py -- it
does not relax any risk rule. A candidate must already pass the full gate
(SignalResult.passed) before it gets scored at all; a high score never
substitutes for a failed check.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from strategy.config import StrategyConfig, DEFAULT_CONFIG
from strategy.signals import SignalResult

SCORE_THRESHOLD = 70.0  # only candidates at/above this are draft-worthy
# Recalibrated 2026-08-04 from 90.0: backtested against 506 gate-passing bars
# across 85 symbols over ~1 year, the maximum score EVER achieved was 82.3 --
# 90 was unreachable in practice (maxing all four 25-point components at once
# essentially never happens in real data), which silently meant the system
# would never trade at all. 70.0 keeps the same "top slice only" intent
# (~5.5% of gate-passing setups clear it) while being an actually-reachable
# bar. See strategy/scoring.py's own history for the backtest that found this.


@dataclass(frozen=True)
class Score:
    total: float
    trend: float
    momentum: float
    volume: float
    risk_quality: float

    @property
    def tradeable(self) -> bool:
        return self.total >= SCORE_THRESHOLD


def score_row(row: pd.Series, sig: SignalResult, config: StrategyConfig = DEFAULT_CONFIG) -> Score:
    """Score a gate-passing row/signal 0-100. Caller must ensure sig.passed
    is True -- an unpassed signal always scores 0."""
    if not sig.passed or sig.side not in ("BUY", "SELL"):
        return Score(0.0, 0.0, 0.0, 0.0, 0.0)

    close = float(row["close"])
    ema_fast, ema_slow = float(row["ema_fast"]), float(row["ema_slow"])
    atr_val = float(row["atr"])
    rsi_val = float(row["rsi"])
    macd_hist = float(row["macd_hist"])
    volume, volume_sma = float(row["volume"]), float(row["volume_sma"])

    # Trend strength (0-25): how far EMA-fast has separated from EMA-slow,
    # normalized by ATR so it's comparable across symbols/prices.
    spread_atr = abs(ema_fast - ema_slow) / atr_val if atr_val > 0 else 0.0
    trend = min(25.0, spread_atr * 5.0)

    # Momentum quality (0-25): reward RSI sitting comfortably inside the
    # directional band (not hugging either edge) plus a strong MACD histogram.
    if sig.side == "BUY":
        band_mid = (config.rsi_buy_min + config.rsi_buy_max) / 2
        band_half = (config.rsi_buy_max - config.rsi_buy_min) / 2
    else:
        band_mid = (config.rsi_sell_min + config.rsi_sell_max) / 2
        band_half = (config.rsi_sell_max - config.rsi_sell_min) / 2
    rsi_centeredness = max(0.0, 1 - abs(rsi_val - band_mid) / band_half) if band_half else 0.0
    hist_strength = min(1.0, abs(macd_hist) / (0.01 * close)) if close > 0 else 0.0
    momentum = 12.5 * rsi_centeredness + 12.5 * hist_strength

    # Volume strength (0-25): how far above its 20-day average today's
    # volume ran, capped so one freak print can't dominate the score.
    vol_ratio = (volume / volume_sma) if volume_sma > 0 else 0.0
    volume_score = min(25.0, max(0.0, (vol_ratio - 1.0) / 2.0 * 25.0))

    # Risk quality (0-25): reward a tight-but-real ATR stop relative to
    # price (more shares per dollar risked, calmer setup); penalize a stop
    # so wide the position gets tiny or the R:R math gets sloppy.
    stop_pct = (config.atr_stop_mult * atr_val / close) if close > 0 else 1.0
    if stop_pct <= 0:
        risk_quality = 0.0
    elif stop_pct < 0.02:
        risk_quality = 25.0 * (stop_pct / 0.02)
    elif stop_pct <= 0.08:
        risk_quality = 25.0
    else:
        risk_quality = max(0.0, 25.0 * (1 - (stop_pct - 0.08) / 0.12))

    total = round(trend + momentum + volume_score + risk_quality, 1)
    return Score(
        total=total,
        trend=round(trend, 1),
        momentum=round(momentum, 1),
        volume=round(volume_score, 1),
        risk_quality=round(risk_quality, 1),
    )
