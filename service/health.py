"""Connectivity, account, and market-data health checks for the
supervisor's monitoring loop. Each returns (ok: bool, message: str).
"""
from __future__ import annotations

from ib_async import IB, Stock

from ibkr.accounts import tag_map


def check_connectivity(ib: IB) -> tuple[bool, str]:
    if not ib.isConnected():
        return False, "not connected to TWS/IB Gateway"
    return True, "connected"


def check_account_health(ib: IB, account: str = "") -> tuple[bool, str]:
    """Health of ONE account.

    accountSummary() reports every account on the login, so an unfiltered
    {tag: value} keeps whichever row arrived last. With a second, empty
    account present that reads as NetLiquidation 0 and the service would
    report the funded account as failing.
    """
    try:
        summary = tag_map(ib.accountSummary(), account)
    except Exception as exc:
        return False, f"accountSummary() failed: {exc}"

    if "NetLiquidation" not in summary:
        return False, ("account summary missing NetLiquidation"
                       + (f" for {account}" if account else ""))
    net_liq = float(summary["NetLiquidation"])
    if net_liq <= 0:
        return False, f"NetLiquidation is non-positive: {net_liq}"

    maint_margin = float(summary.get("MaintMarginReq", 0) or 0)
    excess_liq = float(summary.get("ExcessLiquidity", net_liq) or net_liq)
    if excess_liq <= 0:
        return False, f"ExcessLiquidity is non-positive ({excess_liq}) -- account may be at margin risk"

    return True, f"OK (net liq ${net_liq:.2f}, maint margin ${maint_margin:.2f})"


def check_market_data_health(ib: IB, benchmark_symbol: str = "SPY") -> tuple[bool, str]:
    try:
        contract = Stock(benchmark_symbol, "SMART", "USD")
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            return False, f"could not qualify {benchmark_symbol}"
        [ticker] = ib.reqTickers(qualified[0])
        price = ticker.last if ticker.last and ticker.last > 0 else ticker.marketPrice()
        if not price or price <= 0:
            return False, f"no live quote for {benchmark_symbol} (market data may be down/unsubscribed)"
        return True, f"OK ({benchmark_symbol} last {price:.2f})"
    except Exception as exc:
        return False, f"market data check failed: {exc}"
