"""READ-ONLY check that the ETF bot reads the right account. Places no orders.

    set MEDIK_ETF_ACCOUNT=U26953060
    python examples\\etf_account_check.py

Three things make this safe to run while you are still deciding whether to
trade:

  * it connects with readonly=True, so TWS itself rejects any order this
    process could attempt -- the guarantee is enforced by the broker, not by
    this file being careful;
  * it imports read_portfolio and resolve_account from the live runner rather
    than reimplementing them, so a PASS here is evidence about the code the
    bot actually runs;
  * it never imports or calls the scan loop.

It uses its own client id so it can run alongside anything else you have
connected.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import IB


def _runner():
    """Load the live runner as a module WITHOUT running it."""
    spec = importlib.util.spec_from_file_location(
        "etf_live_readonly", str(Path(__file__).resolve().parent / "medik_etf_live.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    live = _runner()
    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("IBKR_PORT", 7496))
    client_id = int(os.environ.get("IBKR_CLIENT_ID", 77))

    ib = IB()
    print(f"connecting {host}:{port} clientId={client_id} readonly=True")
    ib.connect(host, port, clientId=client_id, timeout=15, readonly=True)
    try:
        managed = list(ib.managedAccounts())
        print(f"MANAGED ACCOUNTS: {managed}")

        account, why = live.resolve_account(managed)
        print(f"SELECTED ACCOUNT: {why}")
        if not account:
            print("RESULT: FAIL — no account selected")
            return 2

        subscribed = live.subscribe_account(ib, account)
        print(f"ACCOUNT UPDATES: {'received' if subscribed else 'NOT RECEIVED'}")

        state = live.read_portfolio(ib, account)
        print(f"NET LIQUIDATION: ${state.net_liquidation:,.2f}")
        print(f"AVAILABLE FUNDS: ${state.available_cash:,.2f}")
        held = [(p.symbol, p.quantity, round(p.market_value, 2))
                for p in state.positions]
        print(f"POSITIONS:       {held or 'none'}")
        print(f"BLOCKING ORDERS: {live.blocking_orders(ib, account) or 'none'}")

        # Show every account so a wrong selection is visible rather than
        # inferred from one number looking plausible.
        print("\nall accounts, for comparison:")
        for acct in managed:
            vals = {v.tag: v.value for v in ib.accountValues()
                    if v.account == acct and v.currency in ("USD", "")}
            print(f"  {acct}  NetLiquidation={vals.get('NetLiquidation', '(none)')}"
                  f"  AvailableFunds={vals.get('AvailableFunds', '(none)')}")

        ok = subscribed and state.net_liquidation > 0
        print(f"\nRESULT: {'PASS' if ok else 'FAIL'} — "
              f"{'balances read correctly' if ok else 'balances still unreadable'}")
        print("no orders were placed; the connection was read-only")
        return 0 if ok else 1
    finally:
        ib.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
