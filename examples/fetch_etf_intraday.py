"""Download and cache intraday ETF bars from TWS, in chunks, to local files.

Run this ON YOUR MACHINE with TWS open:

    python examples/fetch_etf_intraday.py                  # default universe
    python examples/fetch_etf_intraday.py TQQQ SOXL        # a subset
    python examples/fetch_etf_intraday.py --months 6       # how far back

WHY THIS EXISTS
    The backtest needs months of 5-minute bars across many symbols. That is
    far too much data to pass through a model's context, so the pipeline is:

        IBKR -> chunked downloads -> local JSON -> offline backtest -> report

    Only the final metrics are small enough to read. The bars stay on disk.

CHUNKING
    IBKR paces historical requests and caps how much one request may return,
    so this walks backwards in 1-month windows, sleeping between requests. A
    chunk already on disk is skipped, making re-runs cheap and resumable
    after an interruption.

RETENTION
    IBKR does not serve unlimited intraday history and the real limit varies
    by subscription and symbol. This script does NOT assume a period: it
    requests, records whatever comes back, and prints the ACTUAL date range
    obtained per symbol. If you only get six weeks, the report says six
    weeks -- a backtest is not made longer by wishing.

15-MINUTE BARS
    Not downloaded. backtest/medik_etf_bt.py derives them from the 5-minute
    bars by grouping threes within a session, which guarantees alignment and
    avoids a second set of pacing-limited requests.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import Stock

from ibkr.client import IBKRClient

DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "etf_intraday"

DEFAULT_UNIVERSE = ["SNXX", "TQQQ", "SQQQ", "SOXL", "SOXS", "QQQ", "SPY", "IWM"]
FULL_UNIVERSE = DEFAULT_UNIVERSE + ["TNA", "TZA", "LABU", "LABD",
                                    "FAS", "FAZ", "ERX", "ERY",
                                    "SMH", "XLK", "XLF", "XLE"]

PACING_SLEEP_SEC = 11        # IBKR historical pacing: ~6 requests/minute
BAR_SIZE = "5 mins"
CHUNK = "1 M"


def chunk_path(symbol: str, end: datetime) -> Path:
    return DATA_ROOT / symbol / f"5min_{end:%Y%m%d}.json"


def save(path: Path, bars) -> int:
    """Write one chunk as column arrays; skip empty responses."""
    if not bars:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "bar_size": BAR_SIZE,
        "time": [b.date.strftime("%Y-%m-%dT%H:%M:%S") if hasattr(b.date, "strftime")
                 else str(b.date) for b in bars],
        "open": [float(b.open) for b in bars],
        "high": [float(b.high) for b in bars],
        "low": [float(b.low) for b in bars],
        "close": [float(b.close) for b in bars],
        "volume": [float(b.volume or 0) for b in bars],
    }
    lengths = {len(v) for k, v in payload.items() if isinstance(v, list)}
    if len(lengths) != 1:
        print(f"    REFUSING to write ragged chunk {path.name}: {lengths}")
        return 0
    path.write_text(json.dumps(payload))
    return len(bars)


def fetch_symbol(ib, symbol: str, months: int) -> tuple[int, str, str]:
    qualified = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
    if not qualified:
        print(f"  {symbol}: could not qualify contract")
        return 0, "", ""
    contract = qualified[0]

    total, earliest, latest = 0, "", ""
    end = datetime.now()
    for month in range(months):
        path = chunk_path(symbol, end)
        if path.exists():
            existing = json.loads(path.read_text())
            n = len(existing.get("time", []))
            total += n
            if n:
                earliest = min(earliest or existing["time"][0], existing["time"][0])
                latest = max(latest, existing["time"][-1])
            print(f"  {symbol} chunk {month + 1}/{months} cached ({n} bars)")
            end -= timedelta(days=30)
            continue

        try:
            bars = ib.reqHistoricalData(
                contract, endDateTime=end, durationStr=CHUNK,
                barSizeSetting=BAR_SIZE, whatToShow="TRADES",
                useRTH=True, formatDate=1,
            )
        except Exception as exc:
            print(f"  {symbol} chunk {month + 1}: request failed — {exc}")
            break

        n = save(path, bars)
        total += n
        if n:
            earliest = min(earliest or payload_first(path), payload_first(path))
            latest = max(latest, payload_last(path))
            print(f"  {symbol} chunk {month + 1}/{months}: {n} bars "
                  f"{payload_first(path)[:10]} -> {payload_last(path)[:10]}")
        else:
            # An empty response means retention ran out; going further back
            # will not help.
            print(f"  {symbol} chunk {month + 1}: empty — retention limit reached")
            break

        end -= timedelta(days=30)
        ib.sleep(PACING_SLEEP_SEC)

    return total, earliest, latest


def payload_first(path: Path) -> str:
    return json.loads(path.read_text())["time"][0]


def payload_last(path: Path) -> str:
    return json.loads(path.read_text())["time"][-1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="*", help="symbols (default: initial 8)")
    parser.add_argument("--months", type=int, default=6,
                        help="how many 1-month chunks to walk back (default 6)")
    parser.add_argument("--full", action="store_true",
                        help="use the full 20-ETF universe")
    args = parser.parse_args()

    symbols = args.symbols or (FULL_UNIVERSE if args.full else DEFAULT_UNIVERSE)
    print(f"fetching {BAR_SIZE} bars for {len(symbols)} symbols, "
          f"up to {args.months} months back")
    print(f"cache: {DATA_ROOT}\n")

    client = IBKRClient()
    ib = client.connect(retries=3)
    try:
        summary = []
        for symbol in symbols:
            total, first, last = fetch_symbol(ib, symbol, args.months)
            summary.append((symbol, total, first, last))

        print(f"\n{'symbol':<8}{'bars':>8}   actual range obtained")
        print("-" * 56)
        for symbol, total, first, last in summary:
            span = f"{first[:10]} -> {last[:10]}" if total else "NO DATA"
            print(f"{symbol:<8}{total:>8}   {span}")
        print("\nNext: python backtest/medik_etf_bt.py")
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
