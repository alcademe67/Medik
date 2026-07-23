"""Full scan pipeline: IBKR market scanner -> indicator gate -> risk sizing.

Prints every symbol that passes all checks, with the exact entry/stop/target
and share count that respects the 20%-of-available-funds cap and 1:3 R:R.
Does NOT place any orders -- review the output and act deliberately.

Usage (TWS must be open and logged in):
    python examples/run_scan.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibkr.client import IBKRClient
from ibkr.data import fetch_universe
from ibkr.scanner import scan_universe
from strategy.config import DEFAULT_CONFIG
from strategy.risk import RiskRejected, size_position
from strategy.signals import compute_indicator_frame, evaluate


def main() -> None:
    with IBKRClient() as client:
        ib = client.ib

        available_funds = None
        for row in ib.accountSummary():
            if row.tag == "AvailableFunds":
                available_funds = float(row.value)
                break
        if available_funds is None:
            raise RuntimeError("could not read AvailableFunds from TWS")
        print(f"Available funds: {available_funds:.2f}")

        symbols = scan_universe(ib)
        print(f"Scanner returned {len(symbols)} candidates: {symbols}")

        data = fetch_universe(ib, symbols)
        hits = []
        for symbol, df in data.items():
            if len(df) < DEFAULT_CONFIG.warmup_bars:
                continue
            indf = compute_indicator_frame(df, DEFAULT_CONFIG)
            sig = evaluate(indf, DEFAULT_CONFIG)
            if not sig.passed:
                continue
            try:
                plan = size_position(sig.side, sig.entry, sig.stop, available_funds, DEFAULT_CONFIG)
            except RiskRejected as exc:
                print(f"  {symbol}: signal passed but sizing rejected: {exc}")
                continue
            hits.append((symbol, sig, plan))

        if not hits:
            print("\nNo symbol passed the full gate today.")
            return

        print(f"\n{len(hits)} qualifying setup(s):")
        for symbol, sig, plan in hits:
            print(
                f"  {symbol}: {plan.side} {plan.quantity} @ ~{plan.entry:.2f}, "
                f"stop {plan.stop:.2f}, target {plan.target:.2f} "
                f"(risk ${plan.capital_at_risk:.2f}, R:R 1:{plan.risk_reward:.0f}, "
                f"checks: {sig.checks})"
            )


if __name__ == "__main__":
    main()
