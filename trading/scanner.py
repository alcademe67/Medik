"""Market scanner: which USDT pairs are even worth analysing.

Pulls 24h tickers, drops illiquid coins below a volume floor, and ranks
what's left. Ranking here is a *liquidity/activity* prefilter — it decides
what to feed the strategy, NOT what to buy. The buy decision is the
strategy's job, on real candles.
"""

from __future__ import annotations

import logging

import pandas as pd

from trading import config

logger = logging.getLogger(__name__)


def tickers_to_frame(tickers: dict) -> pd.DataFrame:
    """Normalise ccxt's tickers dict into a tidy DataFrame."""
    rows = []
    for symbol, t in tickers.items():
        rows.append(
            {
                "symbol": symbol,
                "last": t.get("last"),
                # quoteVolume is 24h volume in the QUOTE currency (USDT) — the
                # apples-to-apples liquidity measure across pairs.
                "quote_volume": t.get("quoteVolume"),
                "percentage": t.get("percentage"),  # 24h % change
            }
        )
    return pd.DataFrame(rows)


def rank(
    df: pd.DataFrame,
    quote: str | None = None,
    min_volume: float | None = None,
    top_n: int | None = None,
) -> pd.DataFrame:
    """Filter to liquid `quote` pairs and rank by 24h quote volume.

    Pure function (no network) so it is trivially testable.
    """
    quote = quote or config.QUOTE_CURRENCY
    min_volume = config.MIN_DAILY_VOLUME_USDT if min_volume is None else min_volume
    top_n = config.SCANNER_TOP_N if top_n is None else top_n

    out = df.copy()
    out = out[out["symbol"].str.endswith(f"/{quote}")]
    out = out.dropna(subset=["quote_volume"])
    out = out[out["quote_volume"] >= min_volume]
    out = out.sort_values("quote_volume", ascending=False).reset_index(drop=True)
    return out.head(top_n)


def scan(exchange, **kwargs) -> pd.DataFrame:
    """Fetch live tickers and return the ranked, liquid candidate list."""
    frame = tickers_to_frame(exchange.fetch_tickers())
    ranked = rank(frame, **kwargs)
    logger.info("scanner: %d liquid candidates", len(ranked))
    return ranked
