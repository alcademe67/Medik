"""Historical OHLCV retrieval from TWS into strategy-ready DataFrames."""
from __future__ import annotations

import pandas as pd
from ib_async import IB, Stock, util


def fetch_daily_bars(
    ib: IB,
    symbol: str,
    duration: str = "2 Y",
    exchange: str = "SMART",
    currency: str = "USD",
) -> pd.DataFrame:
    """Fetch daily bars for a stock, returned with open/high/low/close/volume
    columns indexed by date -- the exact frame shape strategy.signals and
    backtest.engine expect."""
    contract = Stock(symbol, exchange, currency)
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        raise ValueError(f"could not qualify {symbol}")

    bars = ib.reqHistoricalData(
        qualified[0],
        endDateTime="",
        durationStr=duration,
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
    )
    if not bars:
        raise ValueError(f"no historical data returned for {symbol}")

    df = util.df(bars)
    df = df.set_index(pd.to_datetime(df["date"]))
    return df[["open", "high", "low", "close", "volume"]]


def fetch_universe(ib: IB, symbols: list[str], duration: str = "2 Y") -> dict[str, pd.DataFrame]:
    """Fetch daily bars for many symbols. Symbols that fail (unknown, no
    data permission) are skipped with a warning rather than aborting the
    whole scan."""
    out: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        try:
            out[symbol] = fetch_daily_bars(ib, symbol, duration)
        except Exception as exc:  # noqa: BLE001 - report and continue the sweep
            print(f"warning: skipping {symbol}: {exc}")
    return out
