"""Real-time quotes from IBKR's Client Portal Web API.

WHY THIS EXISTS
    Account U26953060 holds "US Real-Time Non Consolidated Streaming Quotes",
    fee waived. It works -- verified REALTIME for SPY (ARCA), TQQQ (NASDAQ)
    and SNXX (BATS) -- but NOT through the TWS socket API, which returns
    error 10089: "requires additional subscription FOR API". IBKR licenses
    that feed for its own platforms and treats the socket API as third-party
    redistribution. The Client Portal Web API is one of its own platforms, so
    the same free feed flows through it.

    Direct-venue routing was tested first and refuted: requesting IEX, BATS,
    ARCA, NYSE and ISLAND all returned the identical error naming the primary
    exchange. Routing was not the variable; the API boundary was.

SCOPE
    Quotes only. Orders still go through TWS, historical bars still come from
    TWS, and every risk gate is unchanged. This module cannot place an order
    -- it has no code path that writes anything.

NO NEW DEPENDENCIES
    stdlib only. This runs unattended on a machine whose package state nobody
    checks each morning; a missing import at 06:45 is a silent lost day.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

DEFAULT_BASE_URL = "https://localhost:5000/v1/api"

# Client Portal field ids.
FIELD_LAST = "31"
FIELD_BID = "84"
FIELD_ASK = "86"
FIELD_AVAILABILITY = "6509"
FIELD_PRIOR_CLOSE = "7296"
SNAPSHOT_FIELDS = (FIELD_LAST, FIELD_BID, FIELD_ASK, FIELD_AVAILABILITY,
                   FIELD_PRIOR_CLOSE)

# First character of field 6509 says where the price came from. Mapped onto
# the TWS marketDataType numbering so the existing delayed-data veto in
# read_quote() applies unchanged -- one rule, not two that can drift apart.
AVAILABILITY_TO_MD_TYPE = {
    "R": 1,      # real-time
    "Z": 2,      # frozen (last real-time values)
    "D": 3,      # delayed
    "Y": 4,      # frozen delayed
}


class CpApiError(RuntimeError):
    """The gateway could not be reached, or answered with something unusable."""


@dataclass(frozen=True)
class CpQuote:
    """One symbol's quote, shaped like an ib_async Ticker on purpose.

    read_quote() already enforces freshness, spread, crossed markets and
    delayed data. Matching its expected attribute names means this provider
    is checked by exactly the same rules as the TWS one, rather than getting
    its own parallel set that could quietly diverge.
    """
    conid: int
    bid: float
    ask: float
    last: float
    close: float
    availability: str
    marketDataType: int
    time: datetime


def _parse_price(raw) -> float:
    """A Client Portal price string as a float, or 0.0 if it is not a live one.

    IBKR prefixes some values: "C" means the number is derived from the
    previous CLOSE rather than a trade, and "H" means halted. Both are
    returned in the same field as a live price, so stripping the prefix and
    parsing would silently turn yesterday's close into today's last trade.
    They are rejected instead.
    """
    if raw is None:
        return 0.0
    text = str(raw).strip()
    if not text:
        return 0.0
    if text[0] in ("C", "H"):
        return 0.0
    try:
        value = float(text.replace(",", ""))
    except ValueError:
        return 0.0
    return value if value == value and value > 0 else 0.0


def parse_snapshot_row(row: dict, now: datetime | None = None) -> CpQuote | None:
    """One row of the snapshot response as a CpQuote, or None if unusable."""
    conid = row.get("conid") or row.get("conidEx")
    if conid is None:
        return None
    try:
        conid = int(str(conid).split("@")[0])
    except (TypeError, ValueError):
        return None

    availability = str(row.get(FIELD_AVAILABILITY) or "")
    md_type = AVAILABILITY_TO_MD_TYPE.get(availability[:1], 0)

    # Unknown or absent availability is NOT assumed to be real-time. A quote
    # whose provenance cannot be established is exactly the thing that must
    # not reach a live order.
    if md_type == 0:
        md_type = 3

    stamp = row.get("_updated")
    if stamp is not None:
        try:
            when = datetime.fromtimestamp(int(stamp) / 1000.0, tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            when = now or datetime.now(timezone.utc)
    else:
        when = now or datetime.now(timezone.utc)

    return CpQuote(
        conid=conid,
        bid=_parse_price(row.get(FIELD_BID)),
        ask=_parse_price(row.get(FIELD_ASK)),
        last=_parse_price(row.get(FIELD_LAST)),
        close=_parse_price(row.get(FIELD_PRIOR_CLOSE)),
        availability=availability,
        marketDataType=md_type,
        time=when,
    )


class ClientPortalQuotes:
    """Read-only quote client for a locally running Client Portal Gateway."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 10.0,
                 ca_bundle: str = ""):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._ctx = self._build_ssl_context(base_url, ca_bundle)

    @staticmethod
    def _build_ssl_context(base_url: str, ca_bundle: str):
        """TLS context for the gateway.

        The gateway serves a self-signed certificate on localhost. A CA bundle
        is used when one is supplied. Otherwise verification is disabled ONLY
        for a loopback host -- the connection never leaves the machine, so
        there is no network position from which to intercept it. Any other
        host verifies normally and will fail on a bad certificate, because
        there the risk is real.
        """
        host = urllib.parse.urlparse(base_url).hostname or ""
        if ca_bundle:
            return ssl.create_default_context(cafile=ca_bundle)
        if host in ("localhost", "127.0.0.1", "::1"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return ssl.create_default_context()

    def _get(self, path: str, params: dict | None = None):
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout,
                                        context=self._ctx) as response:
                return json.loads(response.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            raise CpApiError(f"GET {path} -> HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CpApiError(
                f"GET {path} -> {type(exc).__name__}: {exc}. Is the Client "
                "Portal Gateway running?") from exc
        except json.JSONDecodeError as exc:
            raise CpApiError(f"GET {path} -> unparseable response") from exc

    def _post(self, path: str):
        request = urllib.request.Request(url=f"{self.base_url}{path}",
                                         data=b"", method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout,
                                        context=self._ctx) as response:
                return json.loads(response.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            raise CpApiError(f"POST {path} -> HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CpApiError(
                f"POST {path} -> {type(exc).__name__}: {exc}. Is the Client "
                "Portal Gateway running?") from exc
        except json.JSONDecodeError as exc:
            raise CpApiError(f"POST {path} -> unparseable response") from exc

    # ------------------------------------------------------------- session

    def auth_status(self) -> tuple[bool, str]:
        """(usable, explanation) for the gateway session.

        Browser-authenticated and expiring, so this is checked before trading
        rather than assumed. `competing` means another session took over the
        connection -- the quotes would keep arriving and would be someone
        else's problem to trust.
        """
        try:
            status = self._post("/iserver/auth/status") or {}
        except CpApiError as exc:
            return False, str(exc)
        if not isinstance(status, dict):
            return False, f"unexpected auth status payload: {status!r}"
        if not status.get("authenticated"):
            return False, ("gateway session is NOT authenticated — log in at "
                           "https://localhost:5000 and try again")
        if status.get("competing"):
            return False, "another session is competing for this connection"
        if not status.get("connected", True):
            return False, "gateway reports it is not connected to IBKR"
        return True, f"authenticated{' (server: ' + str(status.get('serverName')) + ')' if status.get('serverName') else ''}"

    def tickle(self) -> bool:
        """Keep the session alive. False rather than raising -- a failed
        keepalive is reported by auth_status(), not by killing the loop."""
        try:
            self._post("/tickle")
            return True
        except CpApiError:
            return False

    # -------------------------------------------------------------- quotes

    def snapshot(self, conids, retries: int = 2,
                 now: datetime | None = None) -> dict:
        """{conid: CpQuote} for the given contract ids.

        The first call for a conid frequently returns rows without price
        fields: the gateway registers the subscription and populates it
        afterwards. That is documented behaviour, not an error, so an empty
        first response is retried rather than reported as "no data".
        """
        conids = [int(c) for c in conids]
        if not conids:
            return {}
        params = {"conids": ",".join(str(c) for c in conids),
                  "fields": ",".join(SNAPSHOT_FIELDS)}
        out: dict = {}
        for _ in range(max(1, retries + 1)):
            rows = self._get("/iserver/marketdata/snapshot", params) or []
            if not isinstance(rows, list):
                raise CpApiError(f"unexpected snapshot payload: {rows!r}")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                quote = parse_snapshot_row(row, now)
                if quote is not None and (quote.bid or quote.ask or quote.last):
                    out[quote.conid] = quote
            if len(out) == len(conids):
                break
        return out

    def unsubscribe_all(self) -> bool:
        """Release the gateway's market-data subscriptions."""
        try:
            self._get("/iserver/marketdata/unsubscribeall")
            return True
        except CpApiError:
            return False
