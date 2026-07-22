"""Market-data helpers built on top of KuCoinClient."""

from __future__ import annotations

from typing import List, Optional, Tuple

from .client import KuCoinClient

# KuCoin returns candles as arrays in this field order, newest first.
CANDLE_FIELDS = ("time", "open", "close", "high", "low", "volume", "turnover")


def last_price(client: KuCoinClient, symbol: str) -> float:
    ticker = client.ticker(symbol)
    return float(ticker["price"])


def best_bid_ask(client: KuCoinClient, symbol: str) -> Tuple[float, float]:
    ticker = client.ticker(symbol)
    return float(ticker["bestBid"]), float(ticker["bestAsk"])


def recent_candles(
    client: KuCoinClient,
    symbol: str,
    type: str = "1hour",
    limit: int = 100,
    start_at: Optional[int] = None,
    end_at: Optional[int] = None,
) -> List[dict]:
    """Return up to ``limit`` parsed candles, oldest first."""
    raw = client.candles(symbol, type=type, start_at=start_at, end_at=end_at)
    parsed = []
    for row in raw[:limit]:
        candle = dict(zip(CANDLE_FIELDS, row))
        parsed.append(
            {
                "time": int(candle["time"]),
                "open": float(candle["open"]),
                "close": float(candle["close"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "volume": float(candle["volume"]),
                "turnover": float(candle["turnover"]),
            }
        )
    parsed.sort(key=lambda c: c["time"])
    return parsed
