"""Autonomous stock decision engine (position-trading trend).

Implements the frozen spec in docs/stock_strategy_spec.md. Pure functions only:
entry evaluation, risk sizing, and the daily hold/exit decision. No I/O, no
orders -- exactly what the backtest measures and what a live runner would call.

Designed AROUND the failure modes of the repo's prior (RED) stock strategies:
trade only strong liquid trends, hold long so commissions are a small fraction
of the move, cut fast, let winners run. Whether that clears the acceptance
gates is the backtest's job to decide, not this module's to assume.

The entry rule lives in ONE place -- entry_from_row(features, i) -- so the live
wrapper evaluate_stock_entry() and the backtest run the identical logic. Both
read indicator columns produced once by precompute_features(); the backtest
precomputes per symbol so it is not O(n^2).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from strategy.indicators import atr, ema, macd, rsi

# ---- parameters (FROZEN with the spec; never tuned against OOS) -------------
SMA_LONG = 200
SMA_MID = 50
EMA_FAST = 20
RSI_PERIOD = 14
ATR_PERIOD = 14
BREAKOUT_LOOKBACK = 60
PULLBACK_LOOKBACK = 10
VOL_SURGE_MULT = 1.5
RSI_MIN, RSI_MAX = 45.0, 80.0
MAX_EXTENSION = 1.10          # close must be <= 1.10 * SMA_MID
ATR_STOP_MULT = 2.5
ATR_TRAIL_MULT = 3.0
MAX_HOLD_SESSIONS = 120
TREND_BREAK_DAYS = 2          # consecutive closes below SMA_MID -> exit

RISK_PCT = 0.01               # 1.0% of equity per trade
RISK_PCT_MAX = 0.015          # hard ceiling
MAX_POSITION_PCT = 0.25       # per-position notional cap
MAX_DEPLOYED_PCT = 0.80       # portfolio cap (>=20% cash)
MAX_POSITIONS = 3
MIN_PRICE = 10.0
MIN_DOLLAR_VOL = 50_000_000   # 20-day avg dollar volume floor
MIN_HISTORY = SMA_LONG + 10


@dataclass(frozen=True)
class StockSignal:
    passed: bool
    reason: str
    setup: str = ""            # "pullback" | "breakout" | ""
    entry: float = 0.0         # reference close; the fill is modelled separately
    stop: float = 0.0


def precompute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add all indicator columns ONCE (vectorised). Returns a new frame."""
    out = df.copy()
    close, high, low, vol = out["close"], out["high"], out["low"], out["volume"]
    out["sma200"] = close.rolling(SMA_LONG).mean()
    out["sma50"] = close.rolling(SMA_MID).mean()
    out["ema20"] = ema(close, EMA_FAST)
    out["rsi"] = rsi(close, RSI_PERIOD)
    m = macd(close)
    out["macd_line"], out["macd_sig"] = m["macd"], m["signal"]
    out["atr"] = atr(high, low, close, ATR_PERIOD)
    out["dvol20"] = (close * vol).rolling(20).mean()
    out["vsma20"] = vol.rolling(20).mean()
    out["prior_high60"] = high.rolling(BREAKOUT_LOOKBACK).max().shift(1)
    out["low10"] = low.rolling(PULLBACK_LOOKBACK).min()
    return out


def entry_from_row(f: pd.DataFrame, i: int) -> StockSignal:
    """The single entry rule, reading precomputed features at integer row i.
    `f` must already have precompute_features() columns."""
    if i < MIN_HISTORY or i < 1:
        return StockSignal(False, "insufficient history")
    r = f.iloc[i]
    c = float(r["close"]); a = float(r["atr"])
    sma200 = float(r["sma200"]); sma50 = float(r["sma50"]); ema20 = float(r["ema20"])
    if pd.isna(sma200) or pd.isna(sma50) or pd.isna(a) or a <= 0:
        return StockSignal(False, "indicators not ready")
    if c < MIN_PRICE:
        return StockSignal(False, f"price {c:.2f} < {MIN_PRICE}")
    if float(r["dvol20"]) < MIN_DOLLAR_VOL:
        return StockSignal(False, "below liquidity floor")
    if not (c > sma200 and sma50 > sma200):
        return StockSignal(False, "not in an uptrend regime")
    rv = float(r["rsi"])
    if not (RSI_MIN <= rv <= RSI_MAX):
        return StockSignal(False, f"RSI {rv:.0f} outside {RSI_MIN:.0f}-{RSI_MAX:.0f}")
    if not (float(r["macd_line"]) > float(r["macd_sig"])):
        return StockSignal(False, "MACD below signal")
    if c > MAX_EXTENSION * sma50:
        return StockSignal(False, "too extended above 50-SMA")

    recent_low = float(r["low10"])
    touched_50 = sma50 - a <= recent_low <= sma50 + a
    higher_low = float(f.iloc[i]["low"]) > float(f.iloc[i - 1]["low"])
    pullback = touched_50 and higher_low and c > ema20

    prior_high = float(r["prior_high60"])
    breakout = (not pd.isna(prior_high)) and c > prior_high \
        and float(r["volume"]) >= VOL_SURGE_MULT * float(r["vsma20"])

    if not (pullback or breakout):
        return StockSignal(False, "no pullback-reclaim or breakout setup")
    setup = "pullback" if pullback else "breakout"
    stop = max(c - ATR_STOP_MULT * a, recent_low)
    stop = round(min(stop, c - 0.01), 2)
    if stop <= 0 or stop >= c:
        return StockSignal(False, "invalid stop")
    return StockSignal(True, f"{setup} setup, RSI {rv:.0f}", setup, round(c, 2), stop)


def evaluate_stock_entry(df: pd.DataFrame) -> StockSignal:
    """Live convenience wrapper: precompute, then evaluate the LAST row.
    `df` = chronological daily bars (open/high/low/close/volume) up to and
    including the decision day (no future rows)."""
    if len(df) < MIN_HISTORY:
        return StockSignal(False, "insufficient history")
    f = precompute_features(df.reset_index(drop=True))
    return entry_from_row(f, len(f) - 1)


def size_stock(equity: float, available_cash: float, deployed: float,
               entry: float, stop: float, fractional: bool = True) -> float:
    """Shares to buy. Risk-based, then bounded by the per-position notional cap,
    the 80% portfolio cap, and available cash. Fractional (enabled on the live
    account); 0.0 when nothing is affordable/justified."""
    if entry <= 0 or stop <= 0 or stop >= entry or equity <= 0:
        return 0.0
    per_share_risk = entry - stop
    risk_dollars = min(RISK_PCT, RISK_PCT_MAX) * equity
    qty = min(risk_dollars / per_share_risk,
              (MAX_POSITION_PCT * equity) / entry,
              max(0.0, MAX_DEPLOYED_PCT * equity - deployed) / entry,
              max(0.0, available_cash) / entry)
    if not fractional:
        qty = float(int(qty))
    return round(qty, 4) if qty > 0 else 0.0


def stock_exit(bar_open: float, bar_high: float, bar_low: float, bar_close: float,
               stop: float, highest_close: float, atr_now: float,
               closes_below_50: int, sessions_held: int) -> tuple[bool, float, str]:
    """Daily exit decision, stop-first. Returns (exit?, fill_price, reason).
    Priority: hard stop (gap fills at the open) -> ATR trailing stop -> trend
    break (2 closes below the 50-SMA) -> max hold. HOLD otherwise."""
    if bar_low <= stop:
        return True, min(stop, bar_open), "stop"
    if bar_close <= highest_close - ATR_TRAIL_MULT * atr_now:
        return True, bar_close, "atr_trail"
    if closes_below_50 >= TREND_BREAK_DAYS:
        return True, bar_close, "trend_break"
    if sessions_held >= MAX_HOLD_SESSIONS:
        return True, bar_close, "max_hold"
    return False, 0.0, "hold"
