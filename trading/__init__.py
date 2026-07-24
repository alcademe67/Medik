"""Medik trading research engine (Phase 1).

A read-only research toolkit for KuCoin: pull market data, scan/rank USDT
pairs, compute indicators, and backtest strategies with real performance
metrics. NOTHING in this package places an order or moves money — live
execution is a later, deliberately-gated phase.
"""

__all__ = ["config", "exchange", "indicators", "scanner", "strategy", "backtest"]
