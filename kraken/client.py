"""Minimal, read-only Kraken REST client (spot API, https://api.kraken.com/0/).

Read-only by construction: `public()` and `private()` refuse any endpoint
not in PUBLIC_ENDPOINTS / READ_ONLY_PRIVATE. There is no method that takes
an arbitrary path. Widening READ_ONLY_PRIVATE is a deliberate act that
tests/test_kraken.py will flag -- the same "explicit whitelist, never
inference" rule CLAUDE.md applies to core ETFs.

Credentials come from the environment (KRAKEN_API_KEY / KRAKEN_API_SECRET),
never from a file in the repo and never pasted into chat. Use a key with
only "Query Funds" + "Query Open Orders & Trades" permission.

Network: api.kraken.com is blocked from the Claude cloud container, so this
runs on the owner's machine, the same way the TWS scripts do.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

BASE_URL = "https://api.kraken.com"
API_VERSION = "0"

PUBLIC_ENDPOINTS = frozenset({"Time", "SystemStatus", "Assets", "AssetPairs", "Ticker", "OHLC"})

# Read-only private endpoints. Anything that creates, edits or cancels an
# order, or moves funds (AddOrder, EditOrder, CancelOrder, CancelAll,
# Withdraw, WalletTransfer, Stake, ...), is intentionally absent.
READ_ONLY_PRIVATE = frozenset(
    {"Balance", "BalanceEx", "TradeBalance", "OpenOrders", "ClosedOrders", "TradesHistory"}
)

KEY_ENV = "KRAKEN_API_KEY"
SECRET_ENV = "KRAKEN_API_SECRET"

# transport(method, url, headers, body) -> response bytes. Injectable so the
# tests never touch the network.
Transport = Callable[[str, str, dict, "bytes | None"], bytes]


class KrakenError(RuntimeError):
    """Kraken returned a non-empty `error` list, or the response was unusable."""


class ReadOnlyViolation(PermissionError):
    """An endpoint outside the read-only allowlist was requested."""


class MissingCredentials(RuntimeError):
    """A private endpoint was called without KRAKEN_API_KEY / KRAKEN_API_SECRET."""


def sign(urlpath: str, nonce: str, postdata: str, secret_b64: str) -> str:
    """Kraken's API-Sign header.

    base64( HMAC-SHA512( base64decode(secret), urlpath + SHA256(nonce + postdata) ) )
    """
    digest = hashlib.sha256((nonce + postdata).encode()).digest()
    mac = hmac.new(base64.b64decode(secret_b64), urlpath.encode() + digest, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def _urllib_transport(timeout: float) -> Transport:
    def send(method: str, url: str, headers: dict, body: bytes | None) -> bytes:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise KrakenError(f"HTTP {exc.code} from {url}") from exc
        except urllib.error.URLError as exc:
            raise KrakenError(f"cannot reach {url}: {exc.reason}") from exc

    return send


class KrakenClient:
    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        transport: Transport | None = None,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
    ):
        self.api_key = api_key or None
        self.api_secret = api_secret or None
        self._send = transport or _urllib_transport(timeout)
        self.base_url = base_url.rstrip("/")
        self._nonce_lock = threading.Lock()
        self._last_nonce = 0

    @classmethod
    def from_env(cls, **kwargs) -> "KrakenClient":
        return cls(
            api_key=os.environ.get(KEY_ENV, "").strip() or None,
            api_secret=os.environ.get(SECRET_ENV, "").strip() or None,
            **kwargs,
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _nonce(self) -> str:
        # Kraken rejects a nonce that is not strictly greater than the last
        # one used with this key; two calls in the same millisecond would
        # otherwise collide.
        with self._nonce_lock:
            n = max(int(time.time() * 1000), self._last_nonce + 1)
            self._last_nonce = n
            return str(n)

    @staticmethod
    def _unwrap(raw: bytes, endpoint: str):
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise KrakenError(f"{endpoint}: response is not JSON") from exc
        errors = payload.get("error") or []
        if errors:
            raise KrakenError(f"{endpoint}: {', '.join(map(str, errors))}")
        if "result" not in payload:
            raise KrakenError(f"{endpoint}: response has no result")
        return payload["result"]

    def public(self, endpoint: str, **params):
        if endpoint not in PUBLIC_ENDPOINTS:
            raise ReadOnlyViolation(f"{endpoint!r} is not an allowlisted public endpoint")
        url = f"{self.base_url}/{API_VERSION}/public/{endpoint}"
        query = {k: v for k, v in params.items() if v is not None}
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return self._unwrap(self._send("GET", url, {"User-Agent": "medik-kraken-desk"}, None), endpoint)

    def private(self, endpoint: str, **params):
        if endpoint not in READ_ONLY_PRIVATE:
            raise ReadOnlyViolation(
                f"{endpoint!r} is not a read-only endpoint; the Kraken desk does not "
                "place, modify or cancel orders, or move funds"
            )
        if not self.has_credentials:
            raise MissingCredentials(
                f"set {KEY_ENV} and {SECRET_ENV} (a key with only Query Funds + "
                "Query Open Orders & Trades permission)"
            )
        urlpath = f"/{API_VERSION}/private/{endpoint}"
        nonce = self._nonce()
        data = {"nonce": nonce, **{k: v for k, v in params.items() if v is not None}}
        postdata = urllib.parse.urlencode(data)
        headers = {
            "API-Key": self.api_key,
            "API-Sign": sign(urlpath, nonce, postdata, self.api_secret),
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "medik-kraken-desk",
        }
        raw = self._send("POST", self.base_url + urlpath, headers, postdata.encode())
        return self._unwrap(raw, endpoint)
