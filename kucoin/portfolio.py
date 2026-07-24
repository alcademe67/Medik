"""Pure helpers for summarizing KuCoin account balances.

These functions contain no network calls so they can be unit-tested
offline. The live example wires them to a KuCoinClient.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

# Currencies treated as ~1 USD, so no ticker lookup is needed to value them.
STABLES = frozenset({"USDT", "USDC", "USD", "DAI", "TUSD", "FDUSD", "BUSD"})

PriceLookup = Callable[[str], Optional[float]]


def aggregate_by_currency(accounts: List[dict]) -> Dict[str, dict]:
    """Sum balance/available/holds per currency across all account types.

    Currencies whose total balance is zero are omitted.
    """
    totals: Dict[str, dict] = {}
    for account in accounts:
        balance = float(account.get("balance", 0) or 0)
        if balance == 0:
            continue
        currency = account["currency"]
        entry = totals.setdefault(currency, {"balance": 0.0, "available": 0.0, "holds": 0.0})
        entry["balance"] += balance
        entry["available"] += float(account.get("available", 0) or 0)
        entry["holds"] += float(account.get("holds", 0) or 0)
    return totals


def value_in_usdt(currency: str, amount: float, price_lookup: PriceLookup) -> Optional[float]:
    """Estimate the USDT value of ``amount`` of ``currency``.

    Stablecoins are valued 1:1. For everything else ``price_lookup`` is
    consulted; if it returns None (no market / lookup failed) the value is
    None so the caller can exclude it from the total.
    """
    if currency in STABLES:
        return amount
    price = price_lookup(currency)
    if price is None:
        return None
    return amount * price


def summarize(accounts: List[dict], price_lookup: PriceLookup) -> dict:
    """Build a portfolio summary from raw KuCoin accounts.

    Returns a dict with ``rows`` (one per non-zero currency, richest
    first), ``total_usdt`` (sum of priced rows), and ``unpriced`` (list of
    currencies excluded from the total because no USDT price was found).
    """
    totals = aggregate_by_currency(accounts)
    rows = []
    total_usdt = 0.0
    unpriced: List[str] = []
    for currency, entry in totals.items():
        value = value_in_usdt(currency, entry["balance"], price_lookup)
        if value is None:
            unpriced.append(currency)
        else:
            total_usdt += value
        rows.append(
            {
                "currency": currency,
                "balance": entry["balance"],
                "available": entry["available"],
                "usdt_value": value,
            }
        )
    rows.sort(key=lambda r: (r["usdt_value"] is not None, r["usdt_value"] or 0.0), reverse=True)
    return {"rows": rows, "total_usdt": total_usdt, "unpriced": sorted(unpriced)}
