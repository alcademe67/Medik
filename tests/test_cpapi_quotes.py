"""Client Portal quote parsing — the parts that decide what reaches a trade.

No network. Every test drives the pure parsing and session logic with
payloads shaped like the gateway's, because the failure modes worth guarding
are all in interpretation, not transport: a close-derived price read as a
live trade, an unknown provenance assumed real-time, a competing session
whose quotes belong to someone else.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ibkr.cpapi import (
    AVAILABILITY_TO_MD_TYPE, ClientPortalQuotes, CpApiError,
    _parse_price, parse_snapshot_row,
)

NOW = datetime(2026, 8, 26, 17, 30, tzinfo=timezone.utc)


def _row(**over):
    row = {"conid": 756733, "31": "765.62", "84": "765.62", "86": "765.65",
           "6509": "RB", "7296": "765.91"}
    row.update(over)
    return row


# ------------------------------------------------------------ price parsing


def test_a_plain_price_parses():
    assert _parse_price("765.62") == pytest.approx(765.62)
    assert _parse_price("1,234.50") == pytest.approx(1234.50)


def test_a_close_derived_price_is_rejected():
    """IBKR returns "C123.45" in the SAME field as a live price to mean the
    number came from the previous close. Stripping the prefix would turn
    yesterday's close into today's last trade."""
    assert _parse_price("C765.62") == 0.0


def test_a_halted_price_is_rejected():
    assert _parse_price("H765.62") == 0.0


@pytest.mark.parametrize("junk", [None, "", "   ", "n/a", "--", [], {}])
def test_junk_becomes_zero_not_an_exception(junk):
    assert _parse_price(junk) == 0.0


def test_zero_and_negative_are_not_prices():
    assert _parse_price("0") == 0.0
    assert _parse_price("-1") == 0.0


# --------------------------------------------------------- row interpretation


def test_a_realtime_row_maps_to_market_data_type_1():
    """Mapped onto the TWS numbering so read_quote()'s existing delayed veto
    covers this provider too — one rule, not two that can drift."""
    q = parse_snapshot_row(_row(), NOW)
    assert q.marketDataType == 1
    assert (q.bid, q.ask, q.last) == pytest.approx((765.62, 765.65, 765.62))


@pytest.mark.parametrize("code,expected", [("R", 1), ("Z", 2), ("D", 3), ("Y", 4)])
def test_every_availability_code_maps(code, expected):
    assert parse_snapshot_row(_row(**{"6509": code + "B"}), NOW).marketDataType == expected
    assert AVAILABILITY_TO_MD_TYPE[code] == expected


def test_delayed_data_is_labelled_delayed_so_the_veto_fires():
    q = parse_snapshot_row(_row(**{"6509": "DP"}), NOW)
    assert q.marketDataType == 3


@pytest.mark.parametrize("availability", ["", None, "?", "X", "N"])
def test_unknown_provenance_is_treated_as_delayed_not_realtime(availability):
    """A quote whose origin cannot be established is exactly what must not
    reach a live order. Defaulting to real-time would invert that."""
    q = parse_snapshot_row(_row(**{"6509": availability}), NOW)
    assert q.marketDataType == 3, "unknown availability must not read as live"


def test_a_row_without_a_conid_is_dropped():
    assert parse_snapshot_row({"31": "765.62"}, NOW) is None


def test_a_conid_with_an_exchange_suffix_is_parsed():
    assert parse_snapshot_row(_row(conid="756733@ARCA"), NOW).conid == 756733


def test_a_missing_bid_leaves_zero_rather_than_inventing_one():
    q = parse_snapshot_row(_row(**{"84": None}), NOW)
    assert q.bid == 0.0 and q.ask == pytest.approx(765.65)


def test_the_update_timestamp_is_used_when_present():
    stamp_ms = int(NOW.timestamp() * 1000) - 5000
    q = parse_snapshot_row(_row(_updated=stamp_ms), NOW)
    assert abs((NOW - q.time).total_seconds() - 5.0) < 0.01


def test_a_broken_timestamp_falls_back_to_now_rather_than_raising():
    q = parse_snapshot_row(_row(_updated="not-a-number"), NOW)
    assert q.time == NOW


def test_the_quote_has_the_attributes_read_quote_expects():
    """Duck-typed against ib_async's Ticker on purpose: same object shape
    means the same freshness/spread/crossed/delayed checks apply."""
    q = parse_snapshot_row(_row(), NOW)
    for attr in ("bid", "ask", "last", "close", "marketDataType", "time"):
        assert hasattr(q, attr), attr


# ------------------------------------------------------------------ session


class _Client(ClientPortalQuotes):
    """Overrides transport only; all decision logic is the real thing."""

    def __init__(self, status=None, error=None):
        super().__init__("https://localhost:5000/v1/api")
        self._status, self._error = status, error

    def _post(self, path):
        if self._error:
            raise CpApiError(self._error)
        return self._status


def test_an_authenticated_session_is_usable():
    ok, why = _Client({"authenticated": True, "connected": True}).auth_status()
    assert ok and "authenticated" in why


def test_an_unauthenticated_session_is_refused_with_instructions():
    ok, why = _Client({"authenticated": False}).auth_status()
    assert not ok and "log in" in why


def test_a_competing_session_is_refused():
    """Another login took the connection; those quotes are not ours to trust."""
    ok, why = _Client({"authenticated": True, "competing": True}).auth_status()
    assert not ok and "competing" in why


def test_a_disconnected_gateway_is_refused():
    ok, why = _Client({"authenticated": True, "connected": False}).auth_status()
    assert not ok and "not connected" in why


def test_an_unreachable_gateway_is_refused_not_raised():
    ok, why = _Client(error="Connection refused. Is the Client Portal Gateway "
                            "running?").auth_status()
    assert not ok and "Gateway running" in why


def test_a_nonsense_payload_is_refused():
    ok, why = _Client("banana").auth_status()
    assert not ok and "unexpected" in why


# -------------------------------------------------------------------- TLS


def test_localhost_skips_verification_because_the_cert_is_self_signed():
    import ssl
    ctx = ClientPortalQuotes("https://127.0.0.1:5000/v1/api")._ctx
    assert ctx.verify_mode == ssl.CERT_NONE


def test_a_remote_host_still_verifies():
    """The loopback exemption must not become a blanket one — off-machine
    there is a network position to intercept from."""
    import ssl
    ctx = ClientPortalQuotes("https://quotes.example.com/v1/api")._ctx
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


# --------------------------------------------------- it cannot place orders


def test_the_module_has_no_order_path():
    src = open("ibkr/cpapi.py").read()
    for forbidden in ("/iserver/account/", "placeOrder", "orders", "POST /iserver/account"):
        if forbidden == "orders":
            continue
        assert forbidden not in src, forbidden
    assert "/iserver/marketdata" in src
