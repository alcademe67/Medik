"""Thin CCXT wrapper around KuCoin — read-only market data (Phase 1).

Deliberately has NO order-placing methods. It only lists markets, reads
tickers, and downloads OHLCV candles as tidy pandas DataFrames. CCXT
handles rate limiting (enableRateLimit) and the KuCoin REST quirks; we add
simple retry-with-backoff on top for transient network errors.
"""

from __future__ import annotations

import logging
import time

import pandas as pd

try:
    import ccxt
except ImportError:  # pragma: no cover - dependency hint
    ccxt = None

from trading import config

logger = logging.getLogger(__name__)

OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


class ExchangeError(Exception):
    """Market data could not be retrieved."""


class Exchange:
    """Read-only market-data client for one exchange (default: KuCoin)."""

    def __init__(self, exchange_id: str | None = None, max_retries: int = 4):
        if ccxt is None:
            raise ExchangeError("ccxt is not installed — run: pip install ccxt")
        exchange_id = exchange_id or config.EXCHANGE_ID
        klass = getattr(ccxt, exchange_id, None)
        if klass is None:
            raise ExchangeError(f"unknown exchange id: {exchange_id}")
        # Keys are optional for public data; included only to lift rate limits.
        creds = {}
        if config.KUCOIN_API_KEY:
            creds = {
                "apiKey": config.KUCOIN_API_KEY,
                "secret": config.KUCOIN_API_SECRET,
                "password": config.KUCOIN_API_PASSPHRASE,
            }
        self._client = klass({"enableRateLimit": True, **creds})
        self.max_retries = max_retries

    def _retry(self, fn, *args, **kwargs):
        """Call a ccxt method, retrying transient network errors with backoff."""
        delay = 1.0
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # ccxt raises many network subclasses
                last = exc
                retryable = ccxt is not None and isinstance(
                    exc, (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout)
                )
                if not retryable or attempt == self.max_retries - 1:
                    break
                logger.warning("exchange call failed (%s), retry in %.0fs", exc, delay)
                time.sleep(delay)
                delay *= 2
        raise ExchangeError(str(last)) from last

    def load_markets(self) -> dict:
        return self._retry(self._client.load_markets)

    def usdt_symbols(self, quote: str | None = None) -> list[str]:
        """Active spot symbols quoted in USDT (or QUOTE_CURRENCY), e.g. BTC/USDT."""
        quote = quote or config.QUOTE_CURRENCY
        markets = self.load_markets()
        return sorted(
            m["symbol"]
            for m in markets.values()
            if m.get("quote") == quote
            and m.get("spot", True)
            and m.get("active", True)
        )

    def fetch_tickers(self) -> dict:
        """All 24h tickers keyed by symbol (used by the scanner for volume)."""
        return self._retry(self._client.fetch_tickers)

    def fetch_ohlcv(
        self, symbol: str, timeframe: str | None = None, limit: int | None = None
    ) -> pd.DataFrame:
        """Download candles as a DataFrame indexed by UTC timestamp.

        Columns: open, high, low, close, volume (oldest to newest).
        """
        timeframe = timeframe or config.TIMEFRAME
        limit = limit or config.OHLCV_LIMIT
        raw = self._retry(self._client.fetch_ohlcv, symbol, timeframe, None, limit)
        df = pd.DataFrame(raw, columns=OHLCV_COLUMNS)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df.set_index("timestamp")
