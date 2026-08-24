"""Review and act on LIVE-mode setups queued by the background service
(service/supervisor.py in MODE=LIVE) into journal.pending_orders.

This is the human tap the service's LIVE mode stops short of. Nothing here
is submitted without an explicit typed YES per order, same as
place_today_orders.py and protect_positions.py.

Run ON YOUR MACHINE while TWS is open and logged in:

    python examples/review_pending_orders.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import Stock

from ibkr.accounts import order_belongs_to, resolve_account
from ibkr.client import IBKRClient
from strategy import journal


def main() -> None:
    pending = journal.list_pending_orders(status="pending")
    if not pending:
        print("No pending orders queued.")
        return

    client = IBKRClient()  # 127.0.0.1:7496 unless .env overrides
    ib = client.connect(retries=5)
    accounts = list(ib.managedAccounts())
    print(f"Connected. Accounts: {accounts}")
    account, why = resolve_account(accounts)
    print(f"Account: {why}")
    if not account:
        raise SystemExit("REFUSING TO RUN — approving an order into the wrong "
                         "account cannot be undone. Set IBKR_ACCOUNT.")
    if account.startswith("D"):
        print("NOTE: this looks like a PAPER account.")

    print(f"\n{len(pending)} order(s) queued for review:\n")
    for row in pending:
        symbol = row["symbol"]
        qualified = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
        if not qualified:
            print(f"{symbol}: could not qualify contract, skipping")
            continue
        contract = qualified[0]
        [ticker] = ib.reqTickers(contract)
        live_price = ticker.last if ticker.last and ticker.last > 0 else ticker.marketPrice()

        queued_at = row["queued_at"]
        move_note = ""
        if live_price and row["entry"]:
            move_pct = (live_price - row["entry"]) / row["entry"] * 100
            move_note = f"  [live {live_price:.2f}, {move_pct:+.1f}% since queued]"
            if abs(move_pct) >= 3:
                move_note += "  *** moved significantly since queuing -- re-check before placing ***"

        print(f"[{row['id']}] {symbol}: {row['side']} {row['quantity']} @ {row['entry']:.2f} "
              f"stop {row['stop']:.2f} target {row['target']:.2f} "
              f"(score {row['score']}, queued {queued_at}){move_note}")

        answer = input(f"  Type YES to place this {symbol} bracket order, "
                        f"SKIP to discard, or Enter to leave pending: ").strip().upper()
        now = datetime.now(timezone.utc).isoformat()

        if answer in ("YES", "Y"):
            bracket = ib.bracketOrder(
                row["side"], row["quantity"],
                limitPrice=round(row["entry"], 2),
                takeProfitPrice=round(row["target"], 2),
                stopLossPrice=round(row["stop"], 2),
            )
            bracket.parent.tif = "DAY"
            bracket.takeProfit.tif = "GTC"
            bracket.stopLoss.tif = "GTC"
            for order in bracket:
                order.account = account
                ib.placeOrder(contract, order)
            ib.sleep(2)
            journal.mark_pending_order(row["id"], "placed", now)
            print(f"  {symbol} bracket placed.\n")
        elif answer == "SKIP":
            journal.mark_pending_order(row["id"], "skipped", now)
            print(f"  {symbol} skipped.\n")
        else:
            print(f"  {symbol} left pending.\n")

    print("Done. Open orders now:")
    for t in ib.openTrades():
        if not order_belongs_to(t, account):
            continue
        print(f"  {t.contract.symbol}: {t.order.action} {t.order.totalQuantity} "
              f"{t.order.orderType} [{t.orderStatus.status}]")
    client.disconnect()


if __name__ == "__main__":
    main()
