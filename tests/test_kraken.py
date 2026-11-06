"""Tests for the read-only Kraken desk (kraken/).

The most important property is the negative one: the desk cannot trade or
move funds. Those tests come first. Everything runs against a fake
transport -- no network, and api.kraken.com is blocked from CI's cloud
cousins anyway.

Run: python -m pytest tests/test_kraken.py -q
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ibkr.cache import load_bars, save_bars
from kraken.client import (
    PUBLIC_ENDPOINTS,
    READ_ONLY_PRIVATE,
    KrakenClient,
    KrakenError,
    MissingCredentials,
    ReadOnlyViolation,
    sign,
)
from kraken.desk import (
    account_snapshot,
    daily_bars,
    ohlc_frame,
    parse_open_orders,
    parse_ticker,
    pricing_pairs,
)

# Kraken's published API-Sign example (docs.kraken.com, "Authentication").
DOC_SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
DOC_NONCE = "1616492376594"
DOC_POST = "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25"
DOC_SIG = "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="

WRITE_ENDPOINTS = (
    "AddOrder", "AddOrderBatch", "AmendOrder", "EditOrder", "CancelOrder",
    "CancelAll", "CancelAllOrdersAfter", "CancelOrderBatch", "Withdraw",
    "WithdrawCancel", "WalletTransfer", "Stake", "Unstake", "DepositAddresses",
    "Earn/Allocate", "Earn/Deallocate",
)


class FakeTransport:
    """Routes by endpoint name; records every call."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, str, dict, bytes | None]] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        endpoint = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
        resp = self.responses[endpoint]
        return json.dumps(resp if "error" in resp else {"error": [], "result": resp}).encode()


def client(responses=None, keys=True) -> tuple[KrakenClient, FakeTransport]:
    t = FakeTransport(responses or {})
    kw = {"api_key": "KEY", "api_secret": DOC_SECRET} if keys else {}
    return KrakenClient(transport=t, **kw), t


# ------------------------------------------------------------- read-only

def test_allowlists_are_exactly_what_was_reviewed():
    # Widening either set is a deliberate act: change this test with it.
    assert READ_ONLY_PRIVATE == {
        "Balance", "BalanceEx", "TradeBalance", "OpenOrders", "ClosedOrders", "TradesHistory",
    }
    assert PUBLIC_ENDPOINTS == {"Time", "SystemStatus", "Assets", "AssetPairs", "Ticker", "OHLC"}


@pytest.mark.parametrize("endpoint", WRITE_ENDPOINTS)
def test_trading_and_funding_endpoints_are_refused_before_any_request(endpoint):
    c, t = client()
    with pytest.raises(ReadOnlyViolation):
        c.private(endpoint, pair="XBTUSD", type="buy", volume="1")
    with pytest.raises(ReadOnlyViolation):
        c.public(endpoint)
    assert t.calls == []


def test_private_names_are_not_reachable_through_public():
    c, t = client()
    with pytest.raises(ReadOnlyViolation):
        c.public("Balance")
    assert t.calls == []


def test_client_exposes_no_generic_request_method():
    public_attrs = {a for a in dir(KrakenClient) if not a.startswith("_")}
    assert public_attrs == {"public", "private", "from_env", "has_credentials"}


def test_private_without_keys_fails_without_a_request():
    c, t = client(keys=False)
    with pytest.raises(MissingCredentials):
        c.private("Balance")
    assert t.calls == []


def test_from_env_reads_keys(monkeypatch):
    monkeypatch.setenv("KRAKEN_API_KEY", "  k  ")
    monkeypatch.setenv("KRAKEN_API_SECRET", "")
    c = KrakenClient.from_env(transport=FakeTransport({}))
    assert c.api_key == "k" and not c.has_credentials


# --------------------------------------------------------------- signing

def test_signature_matches_krakens_published_example():
    assert sign("/0/private/AddOrder", DOC_NONCE, DOC_POST, DOC_SECRET) == DOC_SIG


def test_private_request_is_signed_post_with_increasing_nonce():
    c, t = client({"Balance": {"ZUSD": "1"}})
    c.private("Balance")
    c.private("Balance")
    nonces = []
    for method, url, headers, body in t.calls:
        assert method == "POST" and url.endswith("/0/private/Balance")
        post = body.decode()
        nonce = urllib.parse.parse_qs(post)["nonce"][0]
        assert headers["API-Key"] == "KEY"
        assert headers["API-Sign"] == sign("/0/private/Balance", nonce, post, DOC_SECRET)
        nonces.append(int(nonce))
    assert nonces[1] > nonces[0]


def test_kraken_error_list_raises():
    c, _ = client({"Ticker": {"error": ["EQuery:Unknown asset pair"]}})
    with pytest.raises(KrakenError, match="Unknown asset pair"):
        c.public("Ticker", pair="NOPE")


# ------------------------------------------------------------------ bars

DAY = 86400
T0 = 1_760_000_000 - (1_760_000_000 % DAY)  # a UTC midnight


def rows(n):
    return [[T0 + i * DAY, str(100 + i), str(110 + i), str(90 + i), str(105 + i), "0", str(1000 + i), 7]
            for i in range(n)]


def test_still_forming_last_bar_is_dropped():
    now = T0 + 2 * DAY + 3600  # an hour into day 3
    frame = ohlc_frame(rows(3), now=now)
    assert len(frame) == 2
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame["close"].iloc[-1] == 106.0


def test_bar_kept_once_its_day_has_closed():
    assert len(ohlc_frame(rows(3), now=T0 + 3 * DAY)) == 3


def test_daily_bars_round_trips_through_ibkr_cache(tmp_path):
    c, t = client({"OHLC": {"XXBTZUSD": rows(5), "last": T0}}, keys=False)
    key, frame = daily_bars(c, "XBTUSD", now=T0 + 10 * DAY)
    assert key == "XXBTZUSD"
    assert "interval=1440" in t.calls[0][1]
    save_bars(frame, "XBTUSD", tmp_path)
    back = load_bars("XBTUSD", tmp_path)
    assert len(back) == 5 and back["volume"].iloc[0] == 1000.0


# ------------------------------------------------------------- quotes etc

TICKER = {
    "XXBTZUSD": {
        "a": ["60010.0", "1", "1.000"], "b": ["59990.0", "2", "2.000"], "c": ["60000.0", "0.1"],
        "v": ["10", "250"], "h": ["61000", "62000"], "l": ["59000", "58000"], "o": "57000.0",
    }
}
PAIRS = {
    "XXBTZUSD": {"altname": "XBTUSD", "wsname": "XBT/USD", "base": "XXBT", "quote": "ZUSD"},
    "XXBTZUSD.d": {"altname": "XBTUSD.d", "base": "XXBT", "quote": "ZUSD"},
    "XETHZUSD": {"altname": "ETHUSD", "wsname": "ETH/USD", "base": "XETH", "quote": "ZUSD"},
    "XXBTZCAD": {"altname": "XBTCAD", "wsname": "XBT/CAD", "base": "XXBT", "quote": "ZCAD"},
}
ASSETS = {
    "XXBT": {"altname": "XBT"}, "XETH": {"altname": "ETH"}, "ZUSD": {"altname": "USD"},
    "ZCAD": {"altname": "CAD"}, "OBSCURE": {"altname": "OBSCURE"},
}


def test_ticker_parsing():
    (q,) = parse_ticker(TICKER, PAIRS)
    assert q.name == "XBT/USD" and q.mid == 60000.0
    assert q.spread_bps == pytest.approx(20 / 60000 * 1e4)
    assert q.change_pct == pytest.approx((60000 / 57000 - 1) * 100)
    assert q.high_24h == 62000.0  # the 24h figure, not today's


def test_pricing_routes_handle_suffixes_quote_and_unlisted():
    altnames = {k: v["altname"] for k, v in ASSETS.items()}
    routes = pricing_pairs(
        {"XXBT": "1", "XBT.M": "1", "ZUSD": "1", "OBSCURE": "1"}, PAIRS, altnames, "ZUSD"
    )
    assert routes == {"XXBT": "XXBTZUSD", "XBT.M": "XXBTZUSD", "ZUSD": "QUOTE", "OBSCURE": None}


def test_account_snapshot_values_and_flags_unpriced():
    c, _ = client({
        "Balance": {"ZUSD": "50.00", "XXBT": "0.001", "XBT.M": "0.0005", "OBSCURE": "3", "XETH": "0"},
        "Assets": ASSETS,
        "AssetPairs": PAIRS,
        "Ticker": TICKER,
    })
    quote_key, holdings = account_snapshot(c, "USD")
    assert quote_key == "ZUSD"
    by = {h.asset: h for h in holdings}
    assert "XETH" not in by  # zero balances dropped
    assert by["XXBT"].value == pytest.approx(60.0)
    assert by["XBT.M"].value == pytest.approx(30.0)
    assert by["ZUSD"].value == 50.0
    assert by["OBSCURE"].value is None
    assert holdings[0].asset == "XXBT"  # sorted by value


def test_open_orders_parsing():
    (o,) = parse_open_orders({"open": {"OABC-123": {
        "status": "open", "opentm": 1_760_000_000.5, "vol": "0.01", "vol_exec": "0.0",
        "descr": {"order": "buy 0.01 XBTUSD @ limit 55000.0"},
    }}})
    assert o.txid == "OABC-123" and o.description.startswith("buy 0.01 XBTUSD")
    assert o.volume == 0.01 and o.filled == 0.0
