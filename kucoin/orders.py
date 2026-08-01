"""Safety-gated order helpers for KuCoin spot trading.

Every order-placing function requires ``confirm=True``. Without it the
order is rejected locally before any network call is made. Cancelling and
listing are not gated: they can only reduce exposure, never add to it.
"""

from __future__ import annotations

import uuid
import warnings
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from .client import KuCoinClient

VALID_SIDES = ("buy", "sell")

# KuCoin caps page size at 500; the page ceiling is a runaway-loop backstop.
ORDER_PAGE_SIZE = 500
MAX_ORDER_PAGES = 100


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
    """Validate a size/price and render it as a plain decimal string.

    KuCoin rejects scientific notation, so small values must not be
    formatted as ``1e-05``. Decimal (not float) is used throughout so the
    digits the caller supplied survive the round trip intact.
    """
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        raise OrderRejected(f"{name} must be numeric, got {value!r}") from None
    if not number.is_finite():
        raise OrderRejected(f"{name} must be a finite number, got {value!r}")
    if number <= 0:
        raise OrderRejected(f"{name} must be > 0, got {value!r}")
    return format(number, "f")


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
    """Return every active order, following KuCoin's pagination.

    ``/api/v1/orders`` is paginated, so reading only the first page
    silently under-reports once more than ``ORDER_PAGE_SIZE`` orders are
    resting. Pages are walked until the reported ``totalPage`` is reached.
    """
    items: List[dict] = []
    page = 1
    while page <= MAX_ORDER_PAGES:
        data = client.list_orders(
            status="active", symbol=symbol, current_page=page, page_size=ORDER_PAGE_SIZE
        )
        if not isinstance(data, dict):
            return list(data or [])
        batch = data.get("items") or []
        items.extend(batch)
        total_page = data.get("totalPage")
        if total_page is not None:
            try:
                if page >= int(total_page):
                    return items
            except (TypeError, ValueError):
                pass
        # No usable totalPage: a short page means we reached the end.
        if len(batch) < ORDER_PAGE_SIZE:
            return items
        page += 1

    warnings.warn(
        f"open_orders stopped after {MAX_ORDER_PAGES} pages "
        f"({len(items)} orders); the list may be incomplete",
        stacklevel=2,
    )
    return items
