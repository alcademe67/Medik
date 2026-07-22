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

from ibkr.data import fetch_universe
from ibkr.scanner import scan_universe
from strategy.config import DEFAULT_CONFIG
from strategy.risk import RiskRejected, size_position
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


def assert_paper_account(ib: IB) -> None:
    accounts = ib.managedAccounts()
    if not accounts or not all(a.startswith("D") for a in accounts):
        ib.disconnect()
        raise SystemExit(
            f"REFUSING TO RUN: managed accounts {accounts} are not all paper "
            "accounts (paper ids start with 'D'). This script never trades live."
        )


def main() -> None:
    ib = IB()
    ib.connect("127.0.0.1", PAPER_PORT, clientId=42, timeout=10)
    assert_paper_account(ib)

    available_funds = None
    for row in ib.accountSummary():
        if row.tag == "AvailableFunds":
            available_funds = float(row.value)
            break
    if available_funds is None:
        raise SystemExit("could not read AvailableFunds")

    held = {p.contract.symbol for p in ib.positions() if p.position != 0}
    pending = {t.contract.symbol for t in ib.openTrades()}
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
            ib.placeOrder(contract, order)
        ib.sleep(1)
        entered += 1
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
