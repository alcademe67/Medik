"""Order execution with mandatory pre-trade safety validation.

Nothing here can spend money unless `place_market_order` is allowed to
(it refuses on the network unless config.LIVE_TRADING is true). On top of
that gate, every BUY is validated first:

  * the symbol is actually tradable
  * size ≥ the exchange's minimum, rounded to its increment
  * order notional ≥ our floor AND the exchange's quote minimum
  * we hold enough quote currency to pay for it
  * no duplicate — not already in this symbol, no active order for it
  * a minimum interval between submissions (rate-limit courtesy)

Failing any check raises ValidationError and NO order is sent.
"""

from __future__ import annotations

import asyncio
import logging
import time

from bot import config, kucoin_client, state

logger = logging.getLogger(__name__)


class ExecutionError(Exception):
    """An order could not be placed."""


class ValidationError(ExecutionError):
    """A pre-trade safety check failed — the order was NOT submitted."""


_last_order_ts = 0.0


async def _respect_rate_limit() -> None:
    global _last_order_ts
    wait = config.RISK.order_min_interval_s - (time.time() - _last_order_ts)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_order_ts = time.time()


def _round_down(value: float, increment: float) -> float:
    if increment and increment > 0:
        return int(value / increment) * increment
    return value


def _fee_in_quote(fee: float, fee_currency: str, avg_price: float, symbol: str) -> float:
    """Value a fill fee in the QUOTE currency (e.g. USDT) so it can be
    subtracted from a quote-denominated P/L.

    KuCoin may charge the fee in the quote currency (sells), the base currency
    (buys), or KCS. Quote is used as-is; a base-currency fee is converted at the
    fill's average price; anything else we cannot value here, so we log a
    warning and count 0 (never a wrong-unit subtraction that corrupts P/L).
    """
    if not fee:
        return 0.0
    base, quote = symbol.split("-")[0], symbol.split("-")[-1]
    if fee_currency in ("", quote):
        return fee
    if fee_currency == base and avg_price > 0:
        return fee * avg_price
    logger.warning(
        "fee of %.8f %s on %s can't be valued in %s — counting 0 for this leg",
        fee, fee_currency, symbol, quote,
    )
    return 0.0


async def _settle_fees(result: dict, symbol: str, action: str, notional_estimate: float) -> float:
    """Return the fee paid for an order (in quote currency) and log the fill.

    Live orders: fetch the REAL fee from KuCoin fills (best-effort — the order
    already executed, so a fills-lookup failure never raises, it logs and
    returns 0). Paper/dry-run orders never touch the exchange, so the fee is
    ESTIMATED as notional × FEE_RATE for realistic paper accounting.
    """
    if result.get("dryRun") is not False:
        fee = notional_estimate * config.FEE_RATE
        logger.info(
            "FILL %s %s (paper) est_fee=%.8f %s on notional %.6f",
            action, symbol, fee, config.QUOTE_CURRENCY, notional_estimate,
        )
        return fee
    order_id = result.get("orderId")
    if not order_id:
        logger.warning("FILL %s %s: live order returned no orderId, cannot fetch fills", action, symbol)
        return 0.0
    try:
        f = await kucoin_client.get_order_fills(order_id)
    except kucoin_client.KucoinError as exc:
        logger.warning("FILL %s %s: could not fetch fills for order %s: %s", action, symbol, order_id, exc)
        return 0.0
    fee_quote = _fee_in_quote(float(f["fee"]), f["fee_currency"], float(f["avg_price"]), symbol)
    logger.info(
        "FILL %s %s size=%.8f avg_price=%.8f fee=%.8f %s (=%.8f %s) fills=%d orderId=%s",
        action, symbol, f["filled_size"], f["avg_price"], f["fee"], f["fee_currency"],
        fee_quote, config.QUOTE_CURRENCY, f["fills"], order_id,
    )
    return fee_quote


async def validate_buy(symbol: str, size: float, ref_price: float, *, live: bool) -> float:
    """Return the (increment-rounded) size if safe, else raise ValidationError.

    Structural checks (tradable, min size, notional floor, our own
    duplicate position) run in every mode. The real-account checks (funds
    on hand, resting exchange orders) only run when `live` — in paper mode
    there is no real money or real order to check against.
    """
    size_in = size
    info = await kucoin_client.get_symbol_info(symbol)
    if not info["enable_trading"]:
        raise ValidationError(f"{symbol} is not tradable on KuCoin right now")

    size = _round_down(size, info["base_increment"])
    notional = size * ref_price
    floor = max(config.RISK.min_order_usdt, info["quote_min_size"])
    # Log the exchange constraints against the requested size so a rejection on
    # a small account (size/notional below the minimum) is obvious in the log.
    logger.info(
        "validate_buy %s: size_in=%.8f rounded=%.8f base_min=%.8f incr=%.8f "
        "notional=%.6f floor=%.6f live=%s",
        symbol, size_in, size, info["base_min_size"], info["base_increment"],
        notional, floor, live,
    )
    if size <= 0 or size < info["base_min_size"]:
        raise ValidationError(f"size {size} below exchange minimum {info['base_min_size']}")

    if notional < floor:
        raise ValidationError(f"order value {notional:.4f} below minimum {floor}")

    # Our own duplicate guard applies in every mode.
    if state.has_open_symbol(symbol):
        raise ValidationError(f"already holding an open position in {symbol}")

    if live:
        quote = symbol.split("-")[-1]
        # Spot market orders draw from the TRADE account (not main/funding).
        available = await kucoin_client.get_available_balance(quote, "trade")
        fee = notional * config.FEE_RATE
        required = notional + fee
        # Right before submit: what we'd spend (#1), what the API returns (#2),
        # which account (#3), and the size/notional (#4).
        logger.info(
            "validate_buy %s FUNDS: size=%.8f notional=%.6f est_fee=%.6f required=%.6f "
            "available(%s trade)=%.6f",
            symbol, size, notional, fee, required, quote, available,
        )
        # Require room for the fee too, so we never submit an order KuCoin will
        # reject with 200004 (cost = notional + fee must fit the balance).
        if required > available:
            raise ValidationError(
                f"insufficient {quote}: order needs {required:.4f} "
                f"(notional {notional:.4f} + fee {fee:.4f}), trade account has {available:.4f}"
            )
        if await kucoin_client.list_active_orders(symbol):
            raise ValidationError(f"an active order already exists for {symbol}")

    return size


async def open_long(
    symbol: str, size: float, ref_price: float, stop: float, target: float, *, live: bool
) -> tuple[int, dict]:
    """Validate → place a market buy → persist the position. Gated + logged.

    The entry fee is settled (real fill fee when live, estimated in paper) and
    stored on the position so it can be deducted from the NET P/L at close.
    """
    size = await validate_buy(symbol, size, ref_price, live=live)
    await _respect_rate_limit()
    result = await kucoin_client.place_market_order(symbol, "buy", size=size)
    entry_fee = await _settle_fees(result, symbol, "BUY", size * ref_price)
    pos = state.Position(
        symbol=symbol, side="buy", size=size, entry_price=ref_price,
        stop=stop, target=target, opened_at=time.time(),
        client_oid=result["order"]["clientOid"], high_water=ref_price,
        entry_fee=entry_fee,
    )
    pos_id = state.add_position(pos)
    logger.info(
        "OPEN %s size=%s entry=%.6f stop=%.6f target=%.6f entry_fee=%.6f %s live=%s",
        symbol, size, ref_price, stop, target, entry_fee, config.QUOTE_CURRENCY,
        not result.get("dryRun"),
    )
    return pos_id, result


async def close_long(position: state.Position, exit_price: float, reason: str) -> tuple[float, dict]:
    """Place a market sell, settle the exit fee, and record the NET realized P/L.

    NET = gross price move − entry fee (paid at open) − exit fee (settled here).
    The gross figure, total fee, and net are all logged.
    """
    await _respect_rate_limit()
    result = await kucoin_client.place_market_order(position.symbol, "sell", size=position.size)
    exit_fee = await _settle_fees(result, position.symbol, "SELL", position.size * exit_price)
    net = state.close_position(
        position.id, exit_price, reason,
        entry_fee=position.entry_fee, exit_fee=exit_fee,
    )
    gross = (exit_price - position.entry_price) * position.size
    total_fee = (position.entry_fee or 0.0) + exit_fee
    logger.info(
        "CLOSE %s reason=%s exit=%.6f gross=%.4f fees=%.4f net=%.4f %s live=%s",
        position.symbol, reason, exit_price, gross, total_fee, net,
        config.QUOTE_CURRENCY, not result.get("dryRun"),
    )
    return net, result
