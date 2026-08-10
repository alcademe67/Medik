"""Manual smoke test for the TWS connection.

Prerequisites:
  - TWS is open and logged into the target account.
  - File > Global Configuration > API > Settings has "Enable ActiveX and
    Socket Clients" checked, with 127.0.0.1 as a trusted IP.
  - .env is configured (copy .env.example) if your host/port/client id differ
    from the defaults.

Usage:
    python examples/connect_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibkr.client import IBKRClient


def main() -> None:
    with IBKRClient() as client:
        print(f"Connected to TWS at {client.host}:{client.port}")

        print("\nAccount summary:")
        for row in client.account_summary():
            print(f"  {row.tag}: {row.value} {row.currency}")

        print("\nPositions:")
        positions = client.positions()
        if not positions:
            print("  (none)")
        for pos in positions:
            print(f"  {pos.contract.symbol}: {pos.position} @ avg cost {pos.avgCost}")


if __name__ == "__main__":
    main()
