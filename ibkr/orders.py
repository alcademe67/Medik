from __future__ import annotations

from ib_async import IB, LimitOrder, MarketOrder, Stock, Trade

VALID_ACTIONS = ("BUY", "SELL")


class OrderRejected(Exception):
    """Raised when a local safeguard blocks an order before it reaches TWS."""


def _qualified_stock(ib: IB, symbol: str, exchange: str, currency: str) -> Stock:
    contract = Stock(symbol, exchange, currency)
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        raise ValueError(f"Could not qualify contract for {symbol} on {exchange}/{currency}")
    return qualified[0]


def place_limit_order(
    ib: IB,
    symbol: str,
    action: str,
    quantity: float,
    limit_price: float,
    exchange: str = "SMART",
    currency: str = "USD",
    confirm: bool = False,
) -> Trade:
    """Submit a live limit order for a stock. This is real money on a live account.

    The call is a no-op (raises OrderRejected) unless confirm=True is passed
    explicitly, so a script can't submit an order by accident.
    """
    if action not in VALID_ACTIONS:
        raise ValueError(f"action must be one of {VALID_ACTIONS}")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if not confirm:
        raise OrderRejected(
            f"Refusing to place {action} {quantity} {symbol} @ {limit_price}: "
            "call with confirm=True to actually submit this live order."
        )

    contract = _qualified_stock(ib, symbol, exchange, currency)
    return place_limit_order_on_contract(
        ib, contract, action, quantity, limit_price, confirm=confirm
    )


def place_limit_order_on_contract(
    ib: IB,
    contract,
    action: str,
    quantity: float,
    limit_price: float,
    confirm: bool = False,
    account: str = "",
) -> Trade:
    """Limit order against an already-qualified contract.

    Use this when the contract came from ib.positions() or another source
    that already carries a conId — re-qualifying by symbol can resolve to a
    different listing (wrong exchange or currency) than the one actually
    held. Same confirm=True gate as place_limit_order.

    `account` names the account to trade in. On a login managing more than
    one, an untagged order is not merely ambiguous: TWS may route it to the
    wrong account or reject it. It is optional so single-account callers are
    unaffected.
    """
    if action not in VALID_ACTIONS:
        raise ValueError(f"action must be one of {VALID_ACTIONS}")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if limit_price != limit_price or limit_price <= 0:  # NaN-safe
        raise ValueError(f"limit_price must be a positive number, got {limit_price!r}")
    if not confirm:
        raise OrderRejected(
            f"Refusing to place {action} {quantity} {contract.symbol} @ {limit_price}: "
            "call with confirm=True to actually submit this live order."
        )

    order = LimitOrder(action, quantity, limit_price)
    if account:
        order.account = account
    trade = ib.placeOrder(contract, order)
    ib.sleep(1)
    return trade


def place_market_order(
    ib: IB,
    symbol: str,
    action: str,
    quantity: float,
    exchange: str = "SMART",
    currency: str = "USD",
    confirm: bool = False,
) -> Trade:
    """Submit a live market order for a stock. Prefer place_limit_order on a live
    account — market orders have no price protection and can fill far from the
    quote you saw. Requires confirm=True."""
    if action not in VALID_ACTIONS:
        raise ValueError(f"action must be one of {VALID_ACTIONS}")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if not confirm:
        raise OrderRejected(
            f"Refusing to place {action} {quantity} {symbol} at market: "
            "call with confirm=True to actually submit this live order."
        )

    contract = _qualified_stock(ib, symbol, exchange, currency)
    order = MarketOrder(action, quantity)
    trade = ib.placeOrder(contract, order)
    ib.sleep(1)
    return trade


def cancel_order(ib: IB, trade: Trade) -> None:
    ib.cancelOrder(trade.order)


def open_trades(ib: IB) -> list[Trade]:
    return ib.openTrades()
