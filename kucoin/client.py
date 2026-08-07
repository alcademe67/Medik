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
from urllib.parse import quote, urlencode

import requests

DEFAULT_BASE_URL = "https://api.kucoin.com"
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 1.0

# KuCoin refuses a request whose KC-API-TIMESTAMP drifts too far from its
# own clock, which is what a laptop with a slightly wrong time looks like.
CLOCK_SKEW_CODES = frozenset({"400002"})
RATE_LIMIT_CODES = frozenset({"429000"})
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Replaying these is harmless: a GET has no effect and cancelling an
# already-cancelled order is a no-op. A POST is deliberately excluded -
# see the comment in _request.
IDEMPOTENT_METHODS = frozenset({"GET", "DELETE"})


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
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        sync_clock: bool = True,
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
        self.retry_backoff = retry_backoff
        self.sync_clock = sync_clock
        self._clock_offset_ms: Optional[int] = None
        self._session = session or requests.Session()

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)

    # ------------------------------------------------------------------
    # Clock
    # ------------------------------------------------------------------

    def synchronize_clock(self) -> int:
        """Measure and store the offset between the local and KuCoin clocks.

        Called lazily before the first signed request. Returns the offset
        in milliseconds (positive when the local clock is behind).
        """
        before = int(time.time() * 1000)
        server = int(self.server_time())
        after = int(time.time() * 1000)
        # Compare against the midpoint of the round trip rather than
        # either end, so network latency does not skew the offset.
        self._clock_offset_ms = server - (before + after) // 2
        return self._clock_offset_ms

    def _now_ms(self) -> int:
        if self._clock_offset_ms is None and self.sync_clock:
            try:
                self.synchronize_clock()
            except (KuCoinAPIError, requests.RequestException, TypeError, ValueError):
                # Never let a failed sync block signing - fall back to the
                # local clock, which is what we would have used anyway.
                self._clock_offset_ms = 0
        return int(time.time() * 1000) + (self._clock_offset_ms or 0)

    # ------------------------------------------------------------------
    # Signing
    # ------------------------------------------------------------------

    def _sign(self, message: str) -> str:
        digest = hmac.new(self.api_secret.encode(), message.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

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
        ts = timestamp or str(self._now_ms())
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

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        auth: bool = False,
    ) -> Any:
        method = method.upper()
        query = ""
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                query = "?" + urlencode(filtered)
        body_str = json.dumps(body) if body is not None else ""
        url = f"{self.base_url}{path}{query}"

        attempt = 0
        resynced = False
        while True:
            # Rebuilt every attempt: the signature covers a timestamp, so a
            # replay has to be signed afresh or KuCoin rejects it as stale.
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
                # A POST that times out may still have reached the exchange,
                # so replaying it could submit a second order. Only methods
                # that are safe to repeat get retried here.
                if method in IDEMPOTENT_METHODS and attempt < self.max_retries:
                    attempt += 1
                    self._backoff(attempt)
                    continue
                raise

            try:
                payload = resp.json()
            except ValueError:
                if self._retryable_status(resp.status_code, method) and attempt < self.max_retries:
                    attempt += 1
                    self._backoff(attempt)
                    continue
                resp.raise_for_status()
                raise KuCoinAPIError(str(resp.status_code), resp.text[:200])

            code = str(payload.get("code"))
            if code == "200000":
                return payload.get("data")

            # A rate-limited request was refused before it executed, so
            # replaying it is safe for any method, POST included.
            if code in RATE_LIMIT_CODES and attempt < self.max_retries:
                attempt += 1
                self._backoff(attempt)
                continue

            # A rejected timestamp means our clock drifted. Re-measure the
            # offset and try once more before giving up.
            if code in CLOCK_SKEW_CODES and auth and not resynced:
                resynced = True
                try:
                    self.synchronize_clock()
                except (KuCoinAPIError, requests.RequestException, TypeError, ValueError):
                    pass
                else:
                    continue

            raise KuCoinAPIError(code, str(payload.get("msg", resp.text[:200])))

    def _retryable_status(self, status_code: int, method: str) -> bool:
        if status_code not in RETRYABLE_STATUS:
            return False
        # 429 means the request was throttled, not executed, so even a POST
        # can be replayed; a 5xx leaves the outcome unknown.
        return status_code == 429 or method in IDEMPOTENT_METHODS

    def _backoff(self, attempt: int) -> None:
        if self.retry_backoff:
            time.sleep(self.retry_backoff * (2 ** (attempt - 1)))

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
        interval: str = "1hour",
        start_at: Optional[int] = None,
        end_at: Optional[int] = None,
    ) -> list:
        """Fetch raw candles. ``interval`` is KuCoin's ``type`` field, e.g. '1hour'."""
        return self._request(
            "GET",
            "/api/v1/market/candles",
            params={"symbol": symbol, "type": interval, "startAt": start_at, "endAt": end_at},
        )

    # ------------------------------------------------------------------
    # Private endpoints (credentials required)
    # ------------------------------------------------------------------

    def accounts(self, currency: Optional[str] = None, account_type: Optional[str] = None) -> list:
        """List accounts. ``account_type`` is KuCoin's ``type``, e.g. 'trade'."""
        return self._request(
            "GET",
            "/api/v1/accounts",
            params={"currency": currency, "type": account_type},
            auth=True,
        )

    def create_order(self, payload: dict) -> dict:
        return self._request("POST", "/api/v1/orders", body=payload, auth=True)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/api/v1/orders/{quote(str(order_id), safe='')}", auth=True)

    def cancel_order(self, order_id: str) -> dict:
        return self._request(
            "DELETE", f"/api/v1/orders/{quote(str(order_id), safe='')}", auth=True
        )

    def list_orders(
        self,
        status: str = "active",
        symbol: Optional[str] = None,
        current_page: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> dict:
        """Return one page of the paginated order list.

        The response envelope carries ``items`` plus ``currentPage`` /
        ``totalPage``; callers wanting every order should use
        ``kucoin.orders.open_orders``, which walks the pages.
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
