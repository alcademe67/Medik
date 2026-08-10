"""Broad-market scanning via TWS's native market scanner.

Instead of maintaining a static ticker list, this asks IBKR's scanner for
the current most-active liquid US stocks, then runs each through the full
indicator gate. The scanner defines the universe dynamically each run.
"""
from __future__ import annotations

from ib_async import IB, ScannerSubscription

DEFAULT_SCAN_CODES = (
    "MOST_ACTIVE",     # highest volume today
    "HOT_BY_VOLUME",   # unusually high volume vs average
    "TOP_PERC_GAIN",   # biggest % gainers
    "TOP_PERC_LOSE",   # biggest % losers (short candidates)
)


def scan_universe(
    ib: IB,
    scan_codes: tuple[str, ...] = DEFAULT_SCAN_CODES,
    max_per_scan: int = 25,
    min_price: float = 5.0,
    max_price: float = 500.0,
) -> list[str]:
    """Return a deduplicated symbol list from IBKR's market scanners.

    min_price filters out sub-$5 names (illiquid/halty), max_price keeps
    position sizing meaningful on a small account.
    """
    symbols: list[str] = []
    seen: set[str] = set()
    for code in scan_codes:
        sub = ScannerSubscription(
            instrument="STK",
            locationCode="STK.US.MAJOR",
            scanCode=code,
            abovePrice=min_price,
            belowPrice=max_price,
            numberOfRows=max_per_scan,
        )
        rows = ib.reqScannerData(sub)
        for row in rows:
            symbol = row.contractDetails.contract.symbol
            if symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    return symbols
