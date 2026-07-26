"""Bridge the trading/ regime framework into the live engine's Signal API.

The live engine speaks in a Signal enum (BUY / SELL / HOLD) per tick. The
regime framework (in the `trading` package) works on an OHLCV DataFrame and
emits per-bar entry/exit booleans after classifying each bar's market regime:

  * bull     -> trend-following (momentum)
  * sideways -> mean reversion
  * high_vol -> breakout
  * bear     -> cash (no entries)

This module adapts one to the other:

    OHLCV rows -> DataFrame -> regime_signals -> map the LATEST bar to a Signal

It ONLY generates a signal — it never sizes, validates, or places an order.
All risk controls, position sizing, stops, logging, and the emergency brake
stay in the engine, untouched.
"""

from __future__ import annotations

import logging

import pandas as pd

from bot.strategy import Signal
from trading import strategy as research
from trading.config import DEFAULT_PARAMS, StrategyParams

logger = logging.getLogger(__name__)

_OHLCV_COLS = ["open", "high", "low", "close", "volume"]


def frame_from_ohlcv(rows: list[dict]) -> pd.DataFrame:
    """Build an OHLCV DataFrame (indexed by candle time) for the indicators.

    `rows` are the dicts returned by `kucoin_client.fetch_ohlcv` (oldest to
    newest). Returns an empty frame with the right columns if there are none.
    """
    if not rows:
        return pd.DataFrame(columns=_OHLCV_COLS)
    df = pd.DataFrame(rows)
    try:
        df.index = pd.to_datetime(df["time"], unit="s", utc=True)
    except (ValueError, TypeError, OverflowError, KeyError):
        pass  # fall back to the default RangeIndex; indicators don't need time
    return df[_OHLCV_COLS]


def signal_from_frame(
    df: pd.DataFrame, params: StrategyParams = DEFAULT_PARAMS
) -> tuple[Signal, str]:
    """Map the latest bar of the regime framework to a (Signal, regime) pair.

    entry on the last bar -> BUY; else exit on the last bar -> SELL; else HOLD.
    Needs enough history to warm up the trend EMA (default 200) before the
    regime label is trustworthy; until then it returns (HOLD, "warming-up").
    """
    if len(df) < params.ema_trend + 2:
        return Signal.HOLD, "warming-up"
    out = research.regime_signals(df, params)
    regime = str(out["regime"].iloc[-1])
    if bool(out["entry"].iloc[-1]):
        return Signal.BUY, regime
    if bool(out["exit"].iloc[-1]):
        return Signal.SELL, regime
    return Signal.HOLD, regime
