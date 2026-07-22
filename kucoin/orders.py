"""Safety-gated order helpers for KuCoin spot trading.

Every order-placing function requires ``confirm=True``. Without it the
order is rejected locally before any network call is made, mirroring the
safeguards used by the IBKR integration in this repository.
"""

from __future__ import annotations

import uuid
from typing import Optional

from .client import KuCoinClient

VALID_SIDES = ("buy", "sell")


class OrderRejected(Exception):
    """Raised when an order fails local validation or lacks confirm=True."""


def _validate_side_and_symbol(symbol: str, side: str) -> str:
    if not symbol or "-" not in symbol:
        raise OrderRejected(f"symbol must look like 'BTC-USDT', got {symbol!r}")
    side = side.lower()
    if side not in VALID_SIDES:
        raise OrderRejected(f"side must be one of {VALID_SIDES}, got {side!r}")
    return side


def _positive_str(name: str, value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise OrderRejected(f"{name} must be numeric, got {value!r}") from None
    if number <= 0:
        raise OrderRejected(f"{name} must be > 0, got {value!r}")
    return str(value)


def place_limit_order(
    client: KuCoinClient,
    symbol: str,
    side: str,
    size,
    price,
    confirm: bool = False,
    client_oid: Optional[str] = None,
) -> dict:
    """Place a spot limit order. Refuses to submit unless confirm=True."""
    side = _validate_side_and_symbol(symbol, side)
    size = _positive_str("size", size)
    price = _positive_str("price", price)
    if not confirm:
        raise OrderRejected("refusing to place a live order without confirm=True")
    payload = {
        "clientOid": client_oid or uuid.uuid4().hex,
        "side": side,
        "symbol": symbol,
        "type": "limit",
        "price": price,
        "size": size,
    }
    return client.create_order(payload)


def place_market_order(
    client: KuCoinClient,
    symbol: str,
    side: str,
    size=None,
    funds=None,
    confirm: bool = False,
    client_oid: Optional[str] = None,
) -> dict:
    """Place a spot market order sized in base units (size) or quote units (funds).

    Exactly one of ``size`` or ``funds`` must be given. Refuses to submit
    unless confirm=True.
    """
    side = _validate_side_and_symbol(symbol, side)
    if (size is None) == (funds is None):
        raise OrderRejected("provide exactly one of size (base units) or funds (quote units)")
    payload = {
        "clientOid": client_oid or uuid.uuid4().hex,
        "side": side,
        "symbol": symbol,
        "type": "market",
    }
    if size is not None:
        payload["size"] = _positive_str("size", size)
    else:
        payload["funds"] = _positive_str("funds", funds)
    if not confirm:
        raise OrderRejected("refusing to place a live order without confirm=True")
    return client.create_order(payload)


def cancel_order(client: KuCoinClient, order_id: str) -> dict:
    if not order_id:
        raise OrderRejected("order_id is required")
    return client.cancel_order(order_id)


def open_orders(client: KuCoinClient, symbol: Optional[str] = None) -> list:
    data = client.list_orders(status="active", symbol=symbol)
    if isinstance(data, dict):
        return data.get("items", [])
    return data
