"""Backtest the strategy over TWS historical data.

Usage (TWS must be open and logged in):
    python examples/run_backtest.py MARA NFLX PLUG
    python examples/run_backtest.py --scan          # backtest today's scanner universe
"""

import sys

from ibkr.client import IBKRClient
from ibkr.data import fetch_universe
from ibkr.scanner import scan_universe
from backtest.engine import run_backtest
from strategy.config import DEFAULT_CONFIG


def main() -> None:
    args = sys.argv[1:]
    with IBKRClient() as client:
        ib = client.ib
        if args and args[0] == "--scan":
            symbols = scan_universe(ib)
        elif args:
            symbols = [a.upper() for a in args]
        else:
            symbols = ["MARA", "NFLX", "PLUG"]
        print(f"Backtesting {len(symbols)} symbols: {symbols}")

        available_funds = 0.0
        for row in ib.accountSummary():
            if row.tag == "AvailableFunds":
                available_funds = float(row.value)
                break
        capital = available_funds if available_funds > 0 else 10_000.0
        print(f"Starting capital: {capital:.2f}")

        data = fetch_universe(ib, symbols)
        result = run_backtest(data, starting_capital=capital, config=DEFAULT_CONFIG)

        print("\nSummary:")
        for key, value in result.summary().items():
            print(f"  {key}: {value}")
        print("\nTrades:")
        for t in result.trades:
            print(
                f"  {t.symbol} {t.side} {t.quantity} @ {t.entry_price:.2f} "
                f"({t.entry_date.date()}) -> {t.exit_price:.2f} ({t.exit_reason}, "
                f"{t.exit_date.date() if t.exit_date is not None else 'open'}), "
                f"pnl {t.realized_pnl:+.2f} ({t.r_multiple:+.2f}R)"
            )


if __name__ == "__main__":
    main()
