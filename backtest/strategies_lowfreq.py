"""Candidate low-frequency strategies, as weight functions for
backtest/lowfreq.run_lowfreq.

Each takes (history, date) where history holds only bars up to `date`, and
returns target weights. None of them can see the future.

These are deliberately well-documented, widely-replicated approaches rather
than novel inventions -- after the 2026-08-05 verdict (a bespoke setup with
profit factor 1.03), the bar is "does this beat buy-and-hold after real
commissions", and the honest way to answer that is to test the standard
answers rather than invent a new one and overfit it to two years of data.
"""
from __future__ import annotations


def _total_return(df, lookback: int) -> float | None:
    """Simple price return over `lookback` bars, or None if too short."""
    if len(df) < lookback + 1:
        return None
    c = df["close"]
    past, now = c.iloc[-(lookback + 1)], c.iloc[-1]
    if past <= 0:
        return None
    return (now / past) - 1.0


def buy_and_hold(symbol: str):
    """The benchmark that actually matters. If a strategy can't beat this
    after costs, it isn't worth running."""
    def fn(history, date):
        return {symbol: 1.0}
    return fn


def sma_timing(symbol: str, sma_bars: int = 200, safe: str | None = None):
    """Meb Faber-style trend timing: hold the asset while its close is above
    its long moving average, otherwise sit in `safe` (or cash).

    The classic result is not higher return but much lower drawdown; it also
    trades only a handful of times a year, which is what makes it viable
    under this account's cost structure.
    """
    def fn(history, date):
        df = history.get(symbol)
        if df is None or len(df) < sma_bars:
            return {}
        sma = df["close"].iloc[-sma_bars:].mean()
        if df["close"].iloc[-1] > sma:
            return {symbol: 1.0}
        return {safe: 1.0} if safe else {}
    return fn


def dual_momentum(risk_assets: list, safe_asset: str | None = None, lookback: int = 252):
    """Antonacci-style dual momentum: pick the strongest risk asset by
    trailing return (relative momentum), but only hold it if that return is
    positive (absolute momentum); otherwise go to the safe asset/cash.
    """
    def fn(history, date):
        scored = []
        for s in risk_assets:
            df = history.get(s)
            if df is None:
                continue
            r = _total_return(df, lookback)
            if r is not None:
                scored.append((r, s))
        if not scored:
            return {}
        scored.sort(reverse=True)
        best_ret, best = scored[0]
        if best_ret <= 0:
            return {safe_asset: 1.0} if safe_asset else {}
        return {best: 1.0}
    return fn


def cross_sectional_momentum(
    top_n: int = 4,
    lookback: int = 252,
    skip_recent: int = 21,
    trend_filter_symbol: str | None = None,
    trend_sma: int = 200,
    min_history: int = 300,
):
    """Classic cross-sectional momentum: rank the universe by trailing
    return (skipping the most recent month, the standard short-term-reversal
    guard) and hold the top N equally weighted.

    trend_filter_symbol adds a market regime filter -- go to cash entirely
    when the broad market is below its long moving average. Momentum's worst
    historical episodes are crashes; this is the usual mitigation.
    """
    def fn(history, date):
        if trend_filter_symbol:
            bench = history.get(trend_filter_symbol)
            if bench is not None and len(bench) >= trend_sma:
                if bench["close"].iloc[-1] <= bench["close"].iloc[-trend_sma:].mean():
                    return {}

        scored = []
        for s, df in history.items():
            if s == trend_filter_symbol or len(df) < min_history:
                continue
            if len(df) < lookback + skip_recent + 1:
                continue
            c = df["close"]
            past = c.iloc[-(lookback + skip_recent + 1)]
            recent = c.iloc[-(skip_recent + 1)]
            if past <= 0:
                continue
            scored.append(((recent / past) - 1.0, s))
        if not scored:
            return {}
        scored.sort(reverse=True)
        picks = [s for _, s in scored[:top_n]]
        if not picks:
            return {}
        w = 1.0 / len(picks)
        return {s: w for s in picks}
    return fn
