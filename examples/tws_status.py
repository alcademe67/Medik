"""Show the account through your own TWS connection, then exit.

    python examples/tws_status.py

Or double-click check_tws.bat at the repo root, which runs this.

This is the diagnostic to run FIRST whenever TWS behaves oddly. It answers,
in one screen:

  * is the API socket actually reachable, and on which port
  * which account is logged in, and is it live or paper
  * what you hold, what it's worth, and how much cash is free
  * how much of the account is deployed against the 80% cap
  * EVERY working order -- the thing get_account_orders under-reports

That last one matters. On 2026-08-06 the MCP connector showed 2 working
orders while TWS showed 6, and the missing ones included a 100-share order
against a 0.09-share position. This script reads the same socket TWS itself
uses, so what it prints is what TWS has.

Read-only: it opens no orders, cancels nothing, and changes nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibkr.client import IBKRClient
from strategy.config import DEFAULT_CONFIG

PORT_NAMES = {
    7496: "TWS live",
    7497: "TWS paper",
    4001: "IB Gateway live",
    4002: "IB Gateway paper",
}


def _num(values: dict, tag: str, default: float = 0.0) -> float:
    try:
        return float(values[tag])
    except (KeyError, TypeError, ValueError):
        return default


def main() -> None:
    client = IBKRClient()
    port_label = PORT_NAMES.get(client.port, "unknown port")
    print(f"Connecting to {client.host}:{client.port} ({port_label})...")

    try:
        ib = client.connect(timeout=10, retries=1, backoff=2.0)
    except Exception as exc:
        print(f"\nCOULD NOT CONNECT: {exc}\n")
        print("Checklist:")
        print("  1. Is TWS (or IB Gateway) open and logged in?")
        print("     Note: only ONE client can be logged into a username at a time —")
        print("     opening the phone app or Gateway logs the desktop out.")
        print("  2. TWS: File > Global Configuration > API > Settings")
        print("       [x] Enable ActiveX and Socket Clients")
        print("       [ ] Read-Only API   (leave UNCHECKED if you want to place orders)")
        print(f"       Socket port must match {client.port}")
        print("  3. Trusted IP 127.0.0.1 is listed.")
        print("  4. If you run Gateway rather than TWS, set IBKR_PORT=4001 in .env.")
        sys.exit(1)

    try:
        accounts = ib.managedAccounts()
        values = {
            row.tag: row.value
            for row in ib.accountValues()
            if row.currency in ("USD", "")
        }

        is_paper = bool(accounts) and all(a.startswith("D") for a in accounts)
        print(f"CONNECTED — account {', '.join(accounts)} ({'PAPER' if is_paper else 'LIVE'})\n")

        net_liq = _num(values, "NetLiquidation")
        cash = _num(values, "TotalCashValue")
        available = _num(values, "AvailableFunds")
        gross = _num(values, "GrossPositionValue")

        print(f"  net liquidation   ${net_liq:>10,.2f}")
        print(f"  cash              ${cash:>10,.2f}")
        print(f"  available funds   ${available:>10,.2f}")
        print(f"  positions value   ${gross:>10,.2f}")
        if net_liq > 0:
            deployed = gross / net_liq
            cap = DEFAULT_CONFIG.max_deployed_pct
            flag = "OK" if deployed <= cap else "OVER CAP"
            print(f"  deployed          {deployed:>10.1%}   (cap {cap:.0%} — {flag})")

        # ib.portfolio() rather than ib.positions(): positions() carries only
        # quantity and average cost, so the only "value" derivable from it is
        # quantity x cost -- i.e. what you PAID, which is not what the holding
        # is worth and disagrees with GrossPositionValue by the unrealized P&L.
        # portfolio() carries live marketPrice/marketValue/unrealizedPNL.
        print("\nPositions:")
        items = [p for p in ib.portfolio() if p.position]
        if not items:
            print("  (none)")
        for item in items:
            sym = item.contract.symbol
            cost = item.position * item.averageCost
            print(
                f"  {sym:<6} {item.position:>12,.4f} sh @ ${item.marketPrice:,.2f}"
                f"   value ${item.marketValue:>10,.2f}"
                f"   cost ${cost:>10,.2f}"
                f"   unreal. ${item.unrealizedPNL:>+8,.2f}"
            )
        if items:
            print("  (avg cost includes commission, so a fresh position starts slightly red)")

        # Working orders: the whole reason this script exists. reqAllOpenOrders
        # asks TWS for orders placed by ANY client, not just this one, so
        # tickets typed by hand in the GUI show up here too.
        print("\nWorking orders:")
        ib.reqAllOpenOrders()
        ib.sleep(1)
        open_trades = [t for t in ib.openTrades() if t.orderStatus.status not in ("Filled", "Cancelled")]
        if not open_trades:
            print("  (none)")
        for trade in open_trades:
            o, c, st = trade.order, trade.contract, trade.orderStatus
            price = o.lmtPrice or o.auxPrice or 0.0
            print(
                f"  {o.action:<4} {o.totalQuantity:>10,.4f} {c.symbol:<6} "
                f"{o.orderType:<4} @ {price:<10,.2f} {o.tif:<3} "
                f"status={st.status} filled={st.filled}"
            )
        if open_trades:
            print("\n  To cancel any of these, use TWS directly or examples/cancel_open_orders.py.")
    finally:
        client.disconnect()
        print("\nDisconnected.")


if __name__ == "__main__":
    main()
