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
from decimal import ROUND_DOWN, Decimal

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


def _fmt_amount(value: float, increment: float) -> str:
    """Floor `value` to a whole multiple of `increment` and return it as a clean
    fixed-point string (no float artifacts, no scientific notation).

    KuCoin rejects an order field whose precision exceeds the symbol's step —
    a base `size` finer than baseIncrement or a `funds` finer than quoteIncrement
    both come back as 400100 "…increment invalid". Rounding a float with plain
    arithmetic (``int(v/inc)*inc``) reintroduces binary-float noise like
    ``8.288999999999999``; formatting through Decimal avoids that. With
    ``increment <= 0`` (unknown) the value is passed through unrounded.
    """
    d = Decimal(str(value))
    if increment and increment > 0:
        step = Decimal(str(increment))
        d = (d / step).to_integral_value(rounding=ROUND_DOWN) * step
        d = d.quantize(step)
    return format(d, "f")


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


# A market order's response can return before KuCoin has recorded its fills, so
# /api/v1/fills briefly reports 0 filled. Re-query a few times before giving up.
_FILL_RETRY_ATTEMPTS = 5
_FILL_RETRY_DELAY_S = 0.6


async def _fetch_fills_with_retry(order_id: str, symbol: str, action: str) -> dict | None:
    """Poll get_order_fills until it reports a fill, or attempts run out.

    Returns the fills dict (possibly still all-zero on the last try) or None if
    every attempt raised. Never raises — the order already executed, so a
    fills-lookup problem must not crash the trade path.
    """
    f = None
    for attempt in range(1, _FILL_RETRY_ATTEMPTS + 1):
        try:
            f = await kucoin_client.get_order_fills(order_id)
        except kucoin_client.KucoinError as exc:
            logger.warning(
                "FILL %s %s: could not fetch fills for order %s (attempt %d/%d): %s",
                action, symbol, order_id, attempt, _FILL_RETRY_ATTEMPTS, exc,
            )
            f = None
        if f and float(f["filled_size"]) > 0:
            return f
        if attempt < _FILL_RETRY_ATTEMPTS:
            await asyncio.sleep(_FILL_RETRY_DELAY_S)
    return f


async def _settle_fees(result: dict, symbol: str, action: str, notional_estimate: float) -> dict:
    """Return {fee, filled_size, avg_price} for an order and log the fill.

    `fee` is valued in the QUOTE currency. Live orders: real values from KuCoin
    fills (best-effort — the order already executed, so a fills-lookup failure
    never raises; it logs and returns zeros). Paper/dry-run orders never touch
    the exchange, so the fee is ESTIMATED as notional × FEE_RATE and filled_size
    is 0 (the caller falls back to the intended size).
    """
    if result.get("dryRun") is not False:
        fee = notional_estimate * config.FEE_RATE
        logger.info(
            "FILL %s %s (paper) est_fee=%.8f %s on notional %.6f",
            action, symbol, fee, config.QUOTE_CURRENCY, notional_estimate,
        )
        return {"fee": fee, "filled_size": 0.0, "avg_price": 0.0}
    order_id = result.get("orderId")
    if not order_id:
        logger.warning("FILL %s %s: live order returned no orderId, cannot fetch fills", action, symbol)
        return {"fee": 0.0, "filled_size": 0.0, "avg_price": 0.0}
    f = await _fetch_fills_with_retry(order_id, symbol, action)
    if not f:
        logger.warning("FILL %s %s: no fills returned for order %s after retries", action, symbol, order_id)
        return {"fee": 0.0, "filled_size": 0.0, "avg_price": 0.0}
    if float(f["filled_size"]) <= 0:
        logger.warning(
            "FILL %s %s: order %s still shows 0 fills after %d attempts — falling back "
            "to intended size (the SELL sizes off the real balance, so this is safe)",
            action, symbol, order_id, _FILL_RETRY_ATTEMPTS,
        )
    fee_quote = _fee_in_quote(float(f["fee"]), f["fee_currency"], float(f["avg_price"]), symbol)
    logger.info(
        "FILL %s %s size=%.8f avg_price=%.8f fee=%.8f %s (=%.8f %s) fills=%d orderId=%s",
        action, symbol, f["filled_size"], f["avg_price"], f["fee"], f["fee_currency"],
        fee_quote, config.QUOTE_CURRENCY, f["fills"], order_id,
    )
    return {"fee": fee_quote, "filled_size": float(f["filled_size"]), "avg_price": float(f["avg_price"])}


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
    info = await kucoin_client.get_symbol_info(symbol)
    await _respect_rate_limit()
    # Place the BUY by FUNDS (quote to spend), NOT by base size. A market buy
    # specified by base size makes KuCoin reserve funds against the order book
    # and can overrun the balance (error 200004). A funds order spends at most
    # the amount we name, so it can never cost more than we have. funds = the
    # sized notional, which already sits inside the cash buffer — floored to the
    # symbol's quoteIncrement so KuCoin doesn't reject it with 400100 "Funds
    # increment invalid" (e.g. 8.2895 when the step is 0.001).
    funds = _fmt_amount(size * ref_price, info["quote_increment"])
    result = await kucoin_client.place_market_order(symbol, "buy", funds=funds)
    settle = await _settle_fees(result, symbol, "BUY", float(funds))
    entry_fee = settle["fee"]

    # Record the base we ACTUALLY hold so the later SELL offloads exactly that.
    # Live: from the fills, conservatively reduced by one taker fee and floored
    # to the lot size, so we never try to sell more base than we own (KuCoin
    # deducts the buy fee from the coin received). Paper, or live with fills not
    # yet visible: the intended size (close_long re-sizes the SELL against the
    # real exchange balance, so an over-recorded size can never oversell).
    if result.get("dryRun") is False and settle["filled_size"] > 0:
        held = settle["filled_size"] * (1 - config.FEE_RATE)
        base_size = _round_down(held, info["base_increment"])
        entry_price = settle["avg_price"] or ref_price
    else:
        base_size = size
        entry_price = ref_price

    pos = state.Position(
        symbol=symbol, side="buy", size=base_size, entry_price=entry_price,
        stop=stop, target=target, opened_at=time.time(),
        client_oid=result["order"]["clientOid"], high_water=entry_price,
        entry_fee=entry_fee,
    )
    pos_id = state.add_position(pos)
    logger.info(
        "OPEN %s funds=%s %s filled=%.8f base_held=%.8f entry=%.6f stop=%.6f "
        "target=%.6f entry_fee=%.6f %s live=%s",
        symbol, funds, config.QUOTE_CURRENCY, settle["filled_size"], base_size,
        entry_price, stop, target, entry_fee, config.QUOTE_CURRENCY, not result.get("dryRun"),
    )
    return pos_id, result


async def close_long(position: state.Position, exit_price: float, reason: str) -> tuple[float, dict]:
    """Place a market sell, settle the exit fee, and record the NET realized P/L.

    NET = gross price move − entry fee (paid at open) − exit fee (settled here).
    The gross figure, total fee, and net are all logged.
    """
    # SELL exactly the base we can actually offload. Live: query the real trade
    # balance and sell min(recorded size, held), floored to the lot size — so a
    # size recorded a hair high (the buy fee is taken in base) never triggers a
    # 200004 oversell, and a genuinely-missing position (coin withdrawn/sold
    # outside the bot) is dropped without a doomed SELL. Paper: the recorded size.
    sell_size = position.size
    sell_arg: str | float = position.size  # what we hand to the exchange
    if config.LIVE_TRADING:
        base = position.symbol.split("-")[0]
        try:
            held = await kucoin_client.get_available_balance(base, "trade")
            info = await kucoin_client.get_symbol_info(position.symbol)
        except kucoin_client.KucoinError as exc:
            logger.warning(
                "CLOSE %s: could not verify %s balance (%s) — skipping sell this tick",
                position.symbol, base, exc,
            )
            return 0.0, {"aborted": True}
        sell_size = _round_down(min(position.size, held), info["base_increment"])
        # Nothing meaningful left to sell (dust, phantom, or below the exchange
        # minimum): drop the position, book no P/L, send no order.
        if sell_size <= 0 or sell_size < info["base_min_size"]:
            state.remove_position(position.id)
            logger.warning(
                "CLOSE %s: exchange holds only %.8f %s (recorded %.8f, min %.8f) — "
                "removed position id=%s, NO sell sent",
                position.symbol, held, base, position.size, info["base_min_size"], position.id,
            )
            return 0.0, {"phantom": True}
        # Clean decimal string at the lot's precision (avoids 400100 on the SELL).
        sell_arg = _fmt_amount(sell_size, info["base_increment"])

    await _respect_rate_limit()
    # SELL by base size (offload exactly the base we confirmed we hold).
    result = await kucoin_client.place_market_order(position.symbol, "sell", size=sell_arg)
    exit_fee = (await _settle_fees(result, position.symbol, "SELL", sell_size * exit_price))["fee"]
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
