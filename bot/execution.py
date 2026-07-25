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


async def validate_buy(symbol: str, size: float, ref_price: float, *, live: bool) -> float:
    """Return the (increment-rounded) size if safe, else raise ValidationError.

    Structural checks (tradable, min size, notional floor, our own
    duplicate position) run in every mode. The real-account checks (funds
    on hand, resting exchange orders) only run when `live` — in paper mode
    there is no real money or real order to check against.
    """
    info = await kucoin_client.get_symbol_info(symbol)
    if not info["enable_trading"]:
        raise ValidationError(f"{symbol} is not tradable on KuCoin right now")

    size = _round_down(size, info["base_increment"])
    if size <= 0 or size < info["base_min_size"]:
        raise ValidationError(f"size {size} below exchange minimum {info['base_min_size']}")

    notional = size * ref_price
    floor = max(config.RISK.min_order_usdt, info["quote_min_size"])
    if notional < floor:
        raise ValidationError(f"order value {notional:.4f} below minimum {floor}")

    # Our own duplicate guard applies in every mode.
    if state.has_open_symbol(symbol):
        raise ValidationError(f"already holding an open position in {symbol}")

    if live:
        quote = symbol.split("-")[-1]
        available = await kucoin_client.get_available_balance(quote)
        if notional > available:
            raise ValidationError(
                f"insufficient {quote}: need {notional:.4f}, have {available:.4f}"
            )
        if await kucoin_client.list_active_orders(symbol):
            raise ValidationError(f"an active order already exists for {symbol}")

    return size


async def open_long(
    symbol: str, size: float, ref_price: float, stop: float, target: float, *, live: bool
) -> tuple[int, dict]:
    """Validate → place a market buy → persist the position. Gated + logged."""
    size = await validate_buy(symbol, size, ref_price, live=live)
    await _respect_rate_limit()
    result = await kucoin_client.place_market_order(symbol, "buy", size=size)
    pos = state.Position(
        symbol=symbol, side="buy", size=size, entry_price=ref_price,
        stop=stop, target=target, opened_at=time.time(),
        client_oid=result["order"]["clientOid"], high_water=ref_price,
    )
    pos_id = state.add_position(pos)
    logger.info(
        "OPEN %s size=%s entry=%.6f stop=%.6f target=%.6f live=%s",
        symbol, size, ref_price, stop, target, not result.get("dryRun"),
    )
    return pos_id, result


async def close_long(position: state.Position, exit_price: float, reason: str) -> tuple[float, dict]:
    """Place a market sell for an open position and record the realized P/L."""
    await _respect_rate_limit()
    result = await kucoin_client.place_market_order(position.symbol, "sell", size=position.size)
    pnl = state.close_position(position.id, exit_price, reason)
    logger.info(
        "CLOSE %s reason=%s exit=%.6f pnl=%.4f live=%s",
        position.symbol, reason, exit_price, pnl, not result.get("dryRun"),
    )
    return pnl, result
