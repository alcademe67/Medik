"""Fully autonomous trading loop — PAPER ACCOUNT ONLY.

Scans the market, runs the indicator gate, sizes under the 20% cap, and
places bracket orders (entry limit + stop loss + 3R take-profit) with NO
per-trade confirmation. Because it is unattended, it hard-refuses to run
against a live account:

  - it connects to the TWS *paper* port (7497), never 7496
  - after connecting it verifies every managed account id starts with "D"
    (IBKR paper prefix) and aborts otherwise

Those two guards are the reason this file is allowed to exist. Prove the
strategy here — weeks of logged results — before any conversation about
automating real money.

Usage (TWS logged into the PAPER account, API enabled on 7497):
    python examples/autotrade_paper.py
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from ib_async import IB

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibkr.accounts import (
    order_belongs_to, positions_for, resolve_account, tag_map,
)
from ibkr.client import IBKRClient
from ibkr.data import fetch_universe
from ibkr.scanner import scan_universe
from strategy.config import DEFAULT_CONFIG
from strategy.risk import RiskRejected, portfolio_headroom, size_position
from strategy.signals import compute_indicator_frame, evaluate

PAPER_PORT = 7497
MAX_NEW_POSITIONS_PER_RUN = 3
MAX_TOTAL_POSITIONS = 5
LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "autotrade_paper.jsonl"


def log_event(event: dict) -> None:
    LOG_PATH.parent.mkdir(exist_ok=True)
    event["ts"] = dt.datetime.now(dt.timezone.utc).isoformat()
    with LOG_PATH.open("a") as fh:
        fh.write(json.dumps(event) + "\n")
    print(event)


def assert_paper_account(ib: IB) -> str:
    """Resolve the account to trade and prove it is a paper account.

    "every account on the login is paper" was the right question when the
    login had one account. With several it is both too weak and too strong:
    too strong because one live account beside a paper one blocks a
    perfectly good paper run, and too weak because it never establishes
    WHICH account the orders would go to. Resolve one, then check that one.
    """
    accounts = list(ib.managedAccounts())
    account, why = resolve_account(accounts)
    if not account:
        ib.disconnect()
        raise SystemExit(f"REFUSING TO RUN: {why}")
    if not account.startswith("D"):
        ib.disconnect()
        raise SystemExit(
            f"REFUSING TO RUN: account {account} is not a paper account "
            "(paper ids start with 'D'). This script never trades live."
        )
    print(f"paper account confirmed: {why}")
    return account


def main() -> None:
    client = IBKRClient(host="127.0.0.1", port=PAPER_PORT, client_id=42)
    ib = client.connect(retries=5)
    account = assert_paper_account(ib)

    summary = tag_map(ib.accountSummary(), account)
    if "AvailableFunds" not in summary or "NetLiquidation" not in summary:
        raise SystemExit(f"could not read account summary values for {account}")
    available_funds = float(summary["AvailableFunds"])
    net_liq = float(summary["NetLiquidation"])
    gross_pos = float(summary.get("GrossPositionValue", 0) or 0)
    headroom = portfolio_headroom(net_liq, gross_pos, DEFAULT_CONFIG)

    held = {p.contract.symbol for p in positions_for(ib, account)}
    pending = {t.contract.symbol for t in ib.openTrades()
               if order_belongs_to(t, account)}
    log_event({"event": "start", "available_funds": available_funds,
               "held": sorted(held), "pending": sorted(pending)})

    if len(held) >= MAX_TOTAL_POSITIONS:
        log_event({"event": "skip_run", "reason": "max total positions reached"})
        ib.disconnect()
        return

    symbols = scan_universe(ib)
    data = fetch_universe(ib, symbols)

    entered = 0
    for symbol, df in data.items():
        if entered >= MAX_NEW_POSITIONS_PER_RUN or len(held) + entered >= MAX_TOTAL_POSITIONS:
            break
        if symbol in held or symbol in pending:
            continue
        if len(df) < DEFAULT_CONFIG.warmup_bars:
            continue

        sig = evaluate(compute_indicator_frame(df, DEFAULT_CONFIG), DEFAULT_CONFIG)
        if not sig.passed:
            continue
        try:
            plan = size_position(sig.side, sig.entry, sig.stop, available_funds, DEFAULT_CONFIG)
        except RiskRejected as exc:
            log_event({"event": "sizing_rejected", "symbol": symbol, "reason": str(exc)})
            continue
        if plan.position_value > headroom:
            log_event({"event": "headroom_rejected", "symbol": symbol,
                       "needed": plan.position_value, "headroom": headroom})
            continue

        contract = data_contract(ib, symbol)
        if contract is None:
            continue
        bracket = ib.bracketOrder(
            plan.side,
            plan.quantity,
            limitPrice=round(plan.entry, 2),
            takeProfitPrice=round(plan.target, 2),
            stopLossPrice=round(plan.stop, 2),
        )
        for order in bracket:
            order.tif = "GTC"
            order.account = account
            ib.placeOrder(contract, order)
        ib.sleep(1)
        entered += 1
        headroom -= plan.position_value
        log_event({
            "event": "bracket_placed", "symbol": symbol, "side": plan.side,
            "qty": plan.quantity, "entry": plan.entry, "stop": plan.stop,
            "target": plan.target, "risk": plan.capital_at_risk,
        })

    log_event({"event": "done", "new_positions": entered})
    ib.disconnect()


def data_contract(ib: IB, symbol: str):
    from ib_async import Stock

    qualified = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
    return qualified[0] if qualified else None


if __name__ == "__main__":
    main()
