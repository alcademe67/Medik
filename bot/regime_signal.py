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

from bot import config
from bot.strategy import Signal
from trading import indicators, strategy as research
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


def min_bars(params: StrategyParams = DEFAULT_PARAMS) -> int:
    """Minimum candles before the regime signal is trusted (leaves warming-up).

    Enough for the rolling windows (Bollinger, volume, breakout) and ADX to
    settle. The trend EMA keeps improving with more history, but it is usable
    well before its full period — so we do NOT wait for the whole 200 bars.
    Requiring that made the bot hang in "warming-up" forever whenever the
    exchange returned fewer candles, so it never traded.
    """
    return max(
        params.ema_slow,
        params.bb_period,
        params.breakout_lookback,
        params.adx_period * 2,
        params.volume_ma,
    ) + 5


def _momentum_signal(df: pd.DataFrame, params: StrategyParams) -> tuple[Signal, str]:
    """Aggressive mode: BUY while a coin is in a short-term uptrend (fast EMA >
    slow EMA), SELL when the trend breaks. Fires on the CURRENT state (not a
    rare cross), so the engine enters uptrending coins right away and rides them
    until the trend flips or the stop/target closes them. Trades far more than
    the regime strategy — and takes weaker setups, so it loses more on
    fees/whipsaw. The ATR stop and profit target still cap every trade."""
    out = indicators.add_indicators(df, params)
    fast = float(out["ema_fast"].iloc[-1])
    slow = float(out["ema_slow"].iloc[-1])
    if fast > slow:
        return Signal.BUY, "momentum-up"
    return Signal.SELL, "momentum-down"


def signal_from_frame(
    df: pd.DataFrame, params: StrategyParams = DEFAULT_PARAMS, mode: str | None = None
) -> tuple[Signal, str]:
    """Map the latest bar to a (Signal, regime) pair.

    mode "regime" (careful) uses the regime framework; mode "momentum"
    (aggressive) rides uptrends. Defaults to config.TRADE_MODE. Needs `min_bars`
    of history for the indicators to be valid; until then returns
    (HOLD, "warming-up").
    """
    if len(df) < min_bars(params):
        return Signal.HOLD, "warming-up"
    if (mode or config.TRADE_MODE) == "momentum":
        return _momentum_signal(df, params)
    out = research.regime_signals(df, params)
    regime = str(out["regime"].iloc[-1])
    if bool(out["entry"].iloc[-1]):
        return Signal.BUY, regime
    if bool(out["exit"].iloc[-1]):
        return Signal.SELL, regime
    return Signal.HOLD, regime
