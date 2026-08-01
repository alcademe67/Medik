"""Minimal KuCoin spot REST API client.

Implements KuCoin API key version 2 request signing as documented at
https://www.kucoin.com/docs (Authentication section). Public market-data
endpoints work without credentials; private endpoints require
KUCOIN_API_KEY / KUCOIN_API_SECRET / KUCOIN_API_PASSPHRASE.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Optional
from urllib.parse import urlencode

import requests

DEFAULT_BASE_URL = "https://api.kucoin.com"
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF = 0.5

# Transport-level statuses worth retrying: rate limiting and gateway blips.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# KuCoin also reports rate limiting in-band, with HTTP 200.
RATE_LIMIT_CODES = frozenset({"429000"})


class KuCoinAPIError(RuntimeError):
    """Raised when the KuCoin API returns a non-success code."""

    def __init__(self, code: str, message: str):
        super().__init__(f"KuCoin API error {code}: {message}")
        self.code = code
        self.message = message


class MissingCredentials(RuntimeError):
    """Raised when a private endpoint is called without API credentials."""


class KuCoinClient:
    """Thin wrapper over the KuCoin spot REST API.

    Credentials fall back to the KUCOIN_API_KEY, KUCOIN_API_SECRET and
    KUCOIN_API_PASSPHRASE environment variables; the base URL falls back
    to KUCOIN_BASE_URL and then the production endpoint.

    Rate-limited and transient failures are retried with exponential
    backoff (``max_retries``/``backoff``). The first signed request
    measures the offset against KuCoin's clock so local drift does not
    invalidate signatures; pass ``auto_sync_time=False`` to skip it.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        api_passphrase: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        session: Optional[requests.Session] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff: float = DEFAULT_BACKOFF,
        auto_sync_time: bool = True,
    ):
        self.api_key = api_key if api_key is not None else os.getenv("KUCOIN_API_KEY", "")
        self.api_secret = api_secret if api_secret is not None else os.getenv("KUCOIN_API_SECRET", "")
        self.api_passphrase = (
            api_passphrase if api_passphrase is not None else os.getenv("KUCOIN_API_PASSPHRASE", "")
        )
        resolved_base = base_url if base_url is not None else os.getenv("KUCOIN_BASE_URL", DEFAULT_BASE_URL)
        self.base_url = resolved_base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.auto_sync_time = auto_sync_time
        self._session = session or requests.Session()
        self._time_offset_ms = 0
        self._time_synced = False

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)

    # ------------------------------------------------------------------
    # Signing
    # ------------------------------------------------------------------

    def _sign(self, message: str) -> str:
        digest = hmac.new(self.api_secret.encode(), message.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def sync_time(self) -> int:
        """Measure and store the offset between the local and KuCoin clocks.

        KuCoin rejects any signed request whose timestamp is more than a
        few seconds off its own clock, so a drifting local clock would
        otherwise fail every private call with an opaque error. Returns
        the offset in milliseconds.
        """
        before = time.time() * 1000
        server_ms = int(self.server_time())
        after = time.time() * 1000
        self._time_offset_ms = int(server_ms - (before + after) / 2)
        self._time_synced = True
        return self._time_offset_ms

    def _timestamp_ms(self) -> str:
        if self.auto_sync_time and not self._time_synced:
            # Only ever attempted once; signing must not fail because the
            # clock probe did. A stale offset of 0 is the old behaviour.
            self._time_synced = True
            try:
                self.sync_time()
            except (KuCoinAPIError, TypeError, ValueError, requests.RequestException):
                self._time_offset_ms = 0
        return str(int(time.time() * 1000) + self._time_offset_ms)

    def _auth_headers(
        self,
        method: str,
        path_with_query: str,
        body: str,
        timestamp: Optional[str] = None,
    ) -> dict:
        if not self.has_credentials:
            raise MissingCredentials(
                "Set KUCOIN_API_KEY, KUCOIN_API_SECRET and KUCOIN_API_PASSPHRASE "
                "to call private endpoints"
            )
        ts = timestamp or self._timestamp_ms()
        str_to_sign = f"{ts}{method.upper()}{path_with_query}{body}"
        return {
            "KC-API-KEY": self.api_key,
            "KC-API-SIGN": self._sign(str_to_sign),
            "KC-API-TIMESTAMP": ts,
            "KC-API-PASSPHRASE": self._sign(self.api_passphrase),
            "KC-API-KEY-VERSION": "2",
        }

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _retry_delay(self, attempt: int, resp: Any = None) -> float:
        """Exponential backoff, unless the server named a Retry-After."""
        retry_after = (getattr(resp, "headers", None) or {}).get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except (TypeError, ValueError):
                pass
        return self.backoff * (2**attempt)

    @staticmethod
    def _parse(resp: Any) -> dict:
        """Return the JSON envelope, or raise KuCoinAPIError describing why not."""
        try:
            payload = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise KuCoinAPIError(str(resp.status_code), resp.text[:200]) from None
        if not isinstance(payload, dict):
            # An error page or proxy can return valid JSON that is not the
            # documented envelope; surface it rather than an AttributeError.
            resp.raise_for_status()
            raise KuCoinAPIError(
                str(resp.status_code), f"unexpected response body: {str(payload)[:200]}"
            )
        return payload

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        auth: bool = False,
    ) -> Any:
        query = ""
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                query = "?" + urlencode(filtered)
        body_str = json.dumps(body) if body is not None else ""
        url = f"{self.base_url}{path}{query}"

        attempt = 0
        while True:
            # Headers are rebuilt per attempt so each retry carries a fresh
            # timestamp; a replayed stale one would fail signature checks.
            headers = {"Content-Type": "application/json"}
            if auth:
                headers.update(self._auth_headers(method, path + query, body_str))
            try:
                resp = self._session.request(
                    method,
                    url,
                    data=body_str if body is not None else None,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException:
                if attempt >= self.max_retries:
                    raise
                time.sleep(self._retry_delay(attempt))
                attempt += 1
                continue

            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                time.sleep(self._retry_delay(attempt, resp))
                attempt += 1
                continue

            payload = self._parse(resp)
            code = str(payload.get("code"))
            if code in RATE_LIMIT_CODES and attempt < self.max_retries:
                time.sleep(self._retry_delay(attempt, resp))
                attempt += 1
                continue
            if code != "200000":
                raise KuCoinAPIError(code, str(payload.get("msg", resp.text[:200])))
            return payload.get("data")

    # ------------------------------------------------------------------
    # Public endpoints (no credentials required)
    # ------------------------------------------------------------------

    def server_time(self) -> int:
        return self._request("GET", "/api/v1/timestamp")

    def symbols(self) -> list:
        return self._request("GET", "/api/v2/symbols")

    def ticker(self, symbol: str) -> dict:
        return self._request("GET", "/api/v1/market/orderbook/level1", params={"symbol": symbol})

    def all_tickers(self) -> dict:
        return self._request("GET", "/api/v1/market/allTickers")

    def candles(
        self,
        symbol: str,
        type: str = "1hour",
        start_at: Optional[int] = None,
        end_at: Optional[int] = None,
    ) -> list:
        return self._request(
            "GET",
            "/api/v1/market/candles",
            params={"symbol": symbol, "type": type, "startAt": start_at, "endAt": end_at},
        )

    # ------------------------------------------------------------------
    # Private endpoints (credentials required)
    # ------------------------------------------------------------------

    def accounts(self, currency: Optional[str] = None, type: Optional[str] = None) -> list:
        return self._request(
            "GET", "/api/v1/accounts", params={"currency": currency, "type": type}, auth=True
        )

    def create_order(self, payload: dict) -> dict:
        return self._request("POST", "/api/v1/orders", body=payload, auth=True)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/api/v1/orders/{order_id}", auth=True)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/api/v1/orders/{order_id}", auth=True)

    def list_orders(
        self,
        status: str = "active",
        symbol: Optional[str] = None,
        current_page: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> dict:
        """Return one page of the paginated order list.

        Callers wanting the full set should use ``orders.open_orders``,
        which walks the pages.
        """
        return self._request(
            "GET",
            "/api/v1/orders",
            params={
                "status": status,
                "symbol": symbol,
                "currentPage": current_page,
                "pageSize": page_size,
            },
            auth=True,
        )
