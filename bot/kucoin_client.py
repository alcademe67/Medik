"""KuCoin exchange API client.

Same layout as api_client.py (the openFDA example): every KuCoin call
lives in this module, handlers only see plain dicts and the exceptions
below.

Public market data needs no credentials. Account endpoints must be
signed with the three credentials KuCoin issues when you create an API
key (key, secret, passphrase) — the scheme from
https://www.kucoin.com/docs is implemented in _signed_headers().
"""

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx

from bot import config

logger = logging.getLogger(__name__)


class KucoinError(Exception):
    """KuCoin could not be reached or returned an unexpected response."""


class AuthError(KucoinError):
    """KuCoin rejected the API credentials (key/secret/passphrase)."""


class NotFoundError(KucoinError):
    """KuCoin has no data for the requested symbol."""


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=config.KUCOIN_BASE_URL,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": "medik-bot/1.0"},
        )
    return _client


async def aclose() -> None:
    """Close the shared client; the bot calls this on shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _signed_headers(method: str, path_with_query: str, body: str = "") -> dict[str, str]:
    """Build the KC-API-* auth headers for a private endpoint.

    KuCoin signs `timestamp + METHOD + path(+query) + body` with the API
    secret (HMAC-SHA256, base64-encoded). The passphrase is signed the
    same way for key versions 2 and 3.
    """
    if not (
        config.KUCOIN_API_KEY
        and config.KUCOIN_API_SECRET
        and config.KUCOIN_API_PASSPHRASE
    ):
        raise AuthError("KuCoin credentials are not set — fill them in in .env first")

    timestamp = str(int(time.time() * 1000))
    secret = config.KUCOIN_API_SECRET.encode()
    prehash = (timestamp + method.upper() + path_with_query + body).encode()
    signature = base64.b64encode(
        hmac.new(secret, prehash, hashlib.sha256).digest()
    ).decode()
    passphrase = base64.b64encode(
        hmac.new(secret, config.KUCOIN_API_PASSPHRASE.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "KC-API-KEY": config.KUCOIN_API_KEY,
        "KC-API-SIGN": signature,
        "KC-API-TIMESTAMP": timestamp,
        "KC-API-PASSPHRASE": passphrase,
        "KC-API-KEY-VERSION": config.KUCOIN_KEY_VERSION,
    }


def _data_or_raise(response: httpx.Response) -> Any:
    """Unwrap KuCoin's {code, msg, data} envelope, raising on any error."""
    if response.status_code == 401:
        try:
            msg = response.json().get("msg", "")
        except ValueError:
            msg = ""
        raise AuthError(msg or "KuCoin rejected the API credentials")
    if response.status_code != 200:
        logger.warning("KuCoin HTTP %s: %s", response.status_code, response.text[:200])
        raise KucoinError(f"KuCoin returned HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise KucoinError("KuCoin sent a non-JSON response") from exc

    code = payload.get("code")
    if code == "400100":  # KuCoin's "invalid parameter", e.g. unknown symbol
        raise NotFoundError(payload.get("msg", "unknown symbol"))
    if code != "200000":
        logger.warning("KuCoin business error %s: %s", code, payload.get("msg"))
        raise KucoinError(payload.get("msg") or f"KuCoin error {code}")
    return payload.get("data")


async def fetch_ticker(symbol: str) -> dict[str, str | None]:
    """24h market snapshot for a trading pair like BTC-USDT (no auth)."""
    try:
        response = await _get_client().get(
            "/api/v1/market/stats", params={"symbol": symbol}
        )
    except httpx.HTTPError as exc:
        logger.warning("KuCoin request failed: %s", exc)
        raise KucoinError("could not reach KuCoin") from exc

    data = _data_or_raise(response)
    if not data or not data.get("last"):
        raise NotFoundError(symbol)
    return {
        "symbol": data.get("symbol") or symbol,
        "last": data.get("last"),
        "change_rate": data.get("changeRate"),
        "high": data.get("high"),
        "low": data.get("low"),
    }


def _positive(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


async def fetch_balances() -> list[dict[str, str]]:
    """All non-zero account balances. Signed; needs "General" permission."""
    path = "/api/v1/accounts"
    headers = _signed_headers("GET", path)
    try:
        response = await _get_client().get(path, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("KuCoin request failed: %s", exc)
        raise KucoinError("could not reach KuCoin") from exc

    accounts = _data_or_raise(response) or []
    balances = [
        {
            "currency": str(account.get("currency", "?")),
            "type": str(account.get("type", "")),
            "balance": str(account.get("balance", "0")),
            "available": str(account.get("available", "0")),
        }
        for account in accounts
        if _positive(account.get("balance"))
    ]
    balances.sort(key=lambda b: b["currency"])
    return balances


async def fetch_klines(
    symbol: str, kline_type: str = "5min", limit: int = 200
) -> list[float]:
    """Recent candles for `symbol`, returned as oldest-to-newest closes.

    Public endpoint, no auth. KuCoin returns newest-first rows of
    [time, open, close, high, low, volume, turnover]; we reverse to
    oldest-first and pull out the close (index 2) for the strategy.
    """
    try:
        response = await _get_client().get(
            "/api/v1/market/candles",
            params={"symbol": symbol, "type": kline_type},
        )
    except httpx.HTTPError as exc:
        logger.warning("KuCoin request failed: %s", exc)
        raise KucoinError("could not reach KuCoin") from exc

    rows = _data_or_raise(response) or []
    closes = [float(row[2]) for row in reversed(rows)]
    return closes[-limit:]


async def place_market_order(
    symbol: str, side: str, *, funds: str | float | None = None,
    size: str | float | None = None,
) -> dict[str, Any]:
    """Place a MARKET order. Signed; requires the Trade permission.

    SAFETY: this refuses to touch the network unless config.LIVE_TRADING
    is true. In dry-run (the default) it returns a simulated result and
    sends nothing — so no bug in any caller can spend real money by
    accident. Exactly one of `funds` (quote amount, for buys) or `size`
    (base amount, for sells) must be given.
    """
    if (funds is None) == (size is None):
        raise ValueError("pass exactly one of funds= or size=")

    order = {
        "clientOid": uuid.uuid4().hex,
        "side": side,
        "symbol": symbol,
        "type": "market",
    }
    if funds is not None:
        order["funds"] = str(funds)
    else:
        order["size"] = str(size)

    if not config.LIVE_TRADING:
        logger.info("DRY-RUN: would place order %s (LIVE_TRADING is false)", order)
        return {"dryRun": True, "order": order}

    path = "/api/v1/orders"
    # The signed body must be byte-for-byte what we send, so build the
    # JSON string once and reuse it for both the signature and the POST.
    body = json.dumps(order, separators=(",", ":"))
    headers = _signed_headers("POST", path, body)
    headers["Content-Type"] = "application/json"
    # [Q7] log the exact outgoing REAL order (no secrets — the signed auth
    # headers are never logged, only the order body).
    logger.info("LIVE ORDER submit -> POST %s body=%s", path, body)
    try:
        response = await _get_client().post(path, headers=headers, content=body)
    except httpx.HTTPError as exc:
        logger.warning("LIVE ORDER network error placing %s: %s", order, exc)
        raise KucoinError("could not reach KuCoin to place order") from exc

    # [Q9] log the COMPLETE raw exchange response (status + full body) BEFORE
    # unwrapping, so a rejection's code/msg is captured verbatim in the log.
    logger.info("LIVE ORDER response <- HTTP %s body=%s", response.status_code, response.text)
    data = _data_or_raise(response) or {}
    logger.info("LIVE ORDER accepted <- orderId=%s", data.get("orderId"))
    return {"dryRun": False, "orderId": data.get("orderId"), "order": order}


async def _signed_get(path: str, params: dict | None = None) -> Any:
    """Signed GET. The query string is signed exactly as it is sent, so the
    signature always matches the request (KuCoin rejects any mismatch).
    """
    query = "?" + urlencode(sorted(params.items())) if params else ""
    headers = _signed_headers("GET", path + query)
    try:
        response = await _get_client().get(path + query, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("KuCoin request failed: %s", exc)
        raise KucoinError("could not reach KuCoin") from exc
    return _data_or_raise(response)


async def get_server_time() -> int:
    """KuCoin server time in ms — a lightweight connectivity + clock check."""
    try:
        response = await _get_client().get("/api/v1/timestamp")
    except httpx.HTTPError as exc:
        raise KucoinError("could not reach KuCoin") from exc
    return int(_data_or_raise(response))


async def get_symbol_info(symbol: str) -> dict[str, Any]:
    """Trading rules for a symbol (min order sizes, increments, tradable)."""
    try:
        response = await _get_client().get("/api/v1/symbols")
    except httpx.HTTPError as exc:
        raise KucoinError("could not reach KuCoin") from exc
    for m in _data_or_raise(response) or []:
        if m.get("symbol") == symbol:
            return {
                "symbol": symbol,
                "base_min_size": float(m.get("baseMinSize") or 0),
                "quote_min_size": float(m.get("quoteMinSize") or 0),
                "base_increment": float(m.get("baseIncrement") or 0),
                "price_increment": float(m.get("priceIncrement") or 0),
                "enable_trading": bool(m.get("enableTrading", False)),
            }
    raise NotFoundError(symbol)


async def get_available_balance(currency: str, account_type: str = "trade") -> float:
    """Available (not held) balance of one currency. Signed."""
    data = await _signed_get("/api/v1/accounts", {"currency": currency, "type": account_type})
    return sum(float(a.get("available") or 0) for a in (data or []))


async def list_active_orders(symbol: str | None = None) -> list[dict]:
    """Open (unfilled) orders, optionally for one symbol. Signed.

    Used by the execution layer to refuse a duplicate order.
    """
    params = {"status": "active"}
    if symbol:
        params["symbol"] = symbol
    data = await _signed_get("/api/v1/orders", params)
    if isinstance(data, dict):  # KuCoin paginates this endpoint
        return data.get("items", [])
    return data or []


async def get_order_fills(order_id: str) -> dict[str, Any]:
    """Aggregate the executed fills for one order. Signed.

    Called right after a live order to record what ACTUALLY happened —
    filled size, volume-weighted average price, and the total fee paid (in
    its fee currency). Returns zeros if the order has no fills yet. KuCoin's
    /api/v1/fills is paginated; we read the first page (a market order for a
    single small position fills in one or a few prints).
    """
    data = await _signed_get("/api/v1/fills", {"orderId": order_id})
    items = data.get("items", []) if isinstance(data, dict) else (data or [])
    size = sum(float(i.get("size") or 0) for i in items)
    funds = sum(float(i.get("funds") or 0) for i in items)
    fee = sum(float(i.get("fee") or 0) for i in items)
    fee_currency = str(items[0].get("feeCurrency", "")) if items else ""
    avg_price = (funds / size) if size > 0 else 0.0
    return {
        "filled_size": size,
        "avg_price": avg_price,
        "fee": fee,
        "fee_currency": fee_currency,
        "fills": len(items),
    }


async def fetch_ohlcv(
    symbol: str, kline_type: str = "5min", limit: int = 400
) -> list[dict[str, float]]:
    """Recent candles as OHLCV dicts, oldest to newest. Public (no auth).

    Full OHLCV including volume — the regime signal needs volume for its
    breakout/volume-confirmation rules, which the (high, low, close)
    `fetch_candles` shape can't supply. KuCoin rows (newest first) are
    [time, open, close, high, low, volume, turnover]; `time` is in seconds.
    """
    try:
        response = await _get_client().get(
            "/api/v1/market/candles", params={"symbol": symbol, "type": kline_type}
        )
    except httpx.HTTPError as exc:
        raise KucoinError("could not reach KuCoin") from exc
    rows = _data_or_raise(response) or []
    out = [
        {
            "time": float(r[0]),
            "open": float(r[1]),
            "close": float(r[2]),
            "high": float(r[3]),
            "low": float(r[4]),
            "volume": float(r[5]),
        }
        for r in reversed(rows)
    ]
    return out[-limit:]


async def fetch_candles(
    symbol: str, kline_type: str = "5min", limit: int = 200
) -> list[tuple[float, float, float]]:
    """Recent candles as (high, low, close) tuples, oldest to newest.

    Used for ATR-based stops. Public endpoint (no auth).
    """
    try:
        response = await _get_client().get(
            "/api/v1/market/candles", params={"symbol": symbol, "type": kline_type}
        )
    except httpx.HTTPError as exc:
        raise KucoinError("could not reach KuCoin") from exc
    # KuCoin rows (newest first): [time, open, close, high, low, volume, turnover]
    rows = _data_or_raise(response) or []
    out = [(float(r[3]), float(r[4]), float(r[2])) for r in reversed(rows)]
    return out[-limit:]
