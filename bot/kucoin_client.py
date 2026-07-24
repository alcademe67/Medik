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
import logging
import time
from typing import Any

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
