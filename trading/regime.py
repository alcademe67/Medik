"""Market-regime detection.

Classifies each bar into one of four regimes so the framework can route to
the right strategy:

  * "high_vol"  — ATR is large relative to price (choppy/violent) -> breakout
  * "bull"      — trending (ADX high) and price above the 200 EMA      -> pullback
  * "bear"      — trending and price below the 200 EMA                 -> cash
  * "sideways"  — no strong trend                                      -> mean reversion

Priority: high volatility first (it overrides trend), then trend direction,
else sideways. Regime is only ever knowable with a lag — this reads the
*recent* state, it can't predict the *next* regime.
"""

from __future__ import annotations

import pandas as pd

from trading import indicators
from trading.config import DEFAULT_PARAMS, StrategyParams


def detect_regime(df: pd.DataFrame, params: StrategyParams = DEFAULT_PARAMS) -> pd.Series:
    """Return a per-bar regime label Series aligned to `df`."""
    out = indicators.add_indicators(df, params)
    atr_pct = out["atr"] / out["close"] * 100

    regime = pd.Series("sideways", index=out.index)
    trending = out["adx"] > params.adx_min
    regime = regime.mask(trending & (out["close"] > out["ema_trend"]), "bull")
    regime = regime.mask(trending & (out["close"] <= out["ema_trend"]), "bear")
    # High volatility overrides trend classification.
    regime = regime.mask(atr_pct > params.high_vol_atr_pct, "high_vol")
    return regime
