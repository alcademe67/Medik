import json
import time
import unittest

import requests

from kucoin.client import KuCoinAPIError, KuCoinClient, MissingCredentials
from kucoin.data import recent_candles
from kucoin.orders import (
    OrderRejected,
    cancel_order,
    open_orders,
    place_limit_order,
    place_market_order,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class FakeSession:
    """Records the request and returns a canned KuCoin success envelope."""

    def __init__(self, data=None):
        self.data = data if data is not None else {"ok": True}
        self.calls = []

    def request(self, method, url, data=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "data": data, "headers": headers})
        return FakeResponse({"code": "200000", "data": self.data})


class FakeOrderClient:
    """Stands in for KuCoinClient in order-helper tests; records submissions."""

    def __init__(self):
        self.orders = []

    def create_order(self, payload):
        self.orders.append(payload)
        return {"orderId": "fake-order-id"}

    def cancel_order(self, order_id):
        return {"cancelledOrderIds": [order_id]}


class SigningTests(unittest.TestCase):
    """Vectors generated independently with hmac/base64 for key 'test-secret'."""

    def make_client(self, **kwargs):
        return KuCoinClient(
            api_key="test-key",
            api_secret="test-secret",
            api_passphrase="test-passphrase",
            **kwargs,
        )

    def test_get_signature(self):
        headers = self.make_client()._auth_headers(
            "GET", "/api/v1/accounts", "", timestamp="1700000000000"
        )
        self.assertEqual(headers["KC-API-SIGN"], "9eOa619WY+scedBCdg8jUC0RJVKphitSmYUHu5N1Cc0=")
        self.assertEqual(headers["KC-API-KEY"], "test-key")
        self.assertEqual(headers["KC-API-TIMESTAMP"], "1700000000000")
        self.assertEqual(headers["KC-API-KEY-VERSION"], "2")

    def test_get_signature_includes_query_string(self):
        headers = self.make_client()._auth_headers(
            "GET",
            "/api/v1/market/orderbook/level1?symbol=BTC-USDT",
            "",
            timestamp="1700000000000",
        )
        self.assertEqual(headers["KC-API-SIGN"], "u0HG/BFovzAHZZZl04U8ZB7hfvFg0vzXKQOowV/ni4o=")

    def test_post_signature_includes_body(self):
        body = (
            '{"clientOid": "abc", "side": "buy", "symbol": "BTC-USDT",'
            ' "type": "limit", "price": "50000", "size": "0.001"}'
        )
        headers = self.make_client()._auth_headers(
            "POST", "/api/v1/orders", body, timestamp="1700000000000"
        )
        self.assertEqual(headers["KC-API-SIGN"], "vhZO1UkpnqC8/bQlL4MZyzWcmH8UET59lQ8Wh2WUS4o=")

    def test_passphrase_is_signed(self):
        headers = self.make_client()._auth_headers(
            "GET", "/api/v1/accounts", "", timestamp="1700000000000"
        )
        self.assertEqual(
            headers["KC-API-PASSPHRASE"], "UbgWiL7WdjQOVBl1OLuMgUbTl9VlKFsjFbLedtCDPrY="
        )

    def test_private_endpoint_without_credentials_raises(self):
        client = KuCoinClient(api_key="", api_secret="", api_passphrase="", session=FakeSession())
        with self.assertRaises(MissingCredentials):
            client.accounts()


class TransportTests(unittest.TestCase):
    def test_query_params_are_appended_and_none_dropped(self):
        session = FakeSession(data={"price": "50000"})
        client = KuCoinClient(api_key="", api_secret="", api_passphrase="", session=session)
        client.candles("BTC-USDT", interval="1hour", start_at=None, end_at=None)
        self.assertEqual(
            session.calls[0]["url"],
            "https://api.kucoin.com/api/v1/market/candles?symbol=BTC-USDT&type=1hour",
        )

    def test_error_code_raises_kucoin_api_error(self):
        class ErrorSession(FakeSession):
            def request(self, *args, **kwargs):
                return FakeResponse({"code": "400100", "msg": "Invalid symbol"})

        client = KuCoinClient(api_key="", api_secret="", api_passphrase="", session=ErrorSession())
        with self.assertRaises(KuCoinAPIError) as ctx:
            client.server_time()
        self.assertEqual(ctx.exception.code, "400100")

    def test_auth_request_carries_signed_headers(self):
        session = FakeSession(data=[])
        client = KuCoinClient(
            api_key="test-key",
            api_secret="test-secret",
            api_passphrase="test-passphrase",
            session=session,
        )
        client.accounts(currency="USDT")
        # calls[0] is the lazy clock sync; the signed request is the last one.
        headers = session.calls[-1]["headers"]
        self.assertIn("KC-API-SIGN", headers)
        self.assertEqual(headers["KC-API-KEY"], "test-key")
        self.assertTrue(session.calls[-1]["url"].endswith("/api/v1/accounts?currency=USDT"))

    def test_order_id_is_url_escaped(self):
        session = FakeSession(data={})
        client = KuCoinClient(
            api_key="k", api_secret="s", api_passphrase="p", session=session, sync_clock=False
        )
        client.cancel_order("weird/id?x=1")
        self.assertTrue(session.calls[-1]["url"].endswith("/api/v1/orders/weird%2Fid%3Fx%3D1"))


class ClockSyncTests(unittest.TestCase):
    """A laptop with a drifted clock must not fail every signed request."""

    class ClockSession:
        """Returns a server time 60s ahead of local on the timestamp endpoint."""

        SKEW_MS = 60_000

        def __init__(self):
            self.calls = []

        def request(self, method, url, data=None, headers=None, timeout=None):
            self.calls.append({"method": method, "url": url, "headers": headers})
            if url.endswith("/api/v1/timestamp"):
                server = int(time.time() * 1000) + self.SKEW_MS
                return FakeResponse({"code": "200000", "data": server})
            return FakeResponse({"code": "200000", "data": []})

    def make_client(self, session, **kwargs):
        return KuCoinClient(
            api_key="k", api_secret="s", api_passphrase="p", session=session, **kwargs
        )

    def test_offset_is_applied_to_signed_timestamp(self):
        session = self.ClockSession()
        client = self.make_client(session)
        client.accounts()
        sent = int(session.calls[-1]["headers"]["KC-API-TIMESTAMP"])
        local = int(time.time() * 1000)
        # The signed timestamp should track KuCoin's clock, not the local one.
        self.assertGreater(sent - local, self.ClockSession.SKEW_MS // 2)

    def test_clock_is_synced_once_not_per_request(self):
        session = self.ClockSession()
        client = self.make_client(session)
        client.accounts()
        client.accounts()
        syncs = [c for c in session.calls if c["url"].endswith("/api/v1/timestamp")]
        self.assertEqual(len(syncs), 1)

    def test_sync_can_be_disabled(self):
        session = self.ClockSession()
        self.make_client(session, sync_clock=False).accounts()
        self.assertEqual([c for c in session.calls if c["url"].endswith("/timestamp")], [])

    def test_failed_sync_falls_back_to_local_clock(self):
        class BrokenClockSession(self.ClockSession):
            def request(self, method, url, data=None, headers=None, timeout=None):
                self.calls.append({"method": method, "url": url, "headers": headers})
                if url.endswith("/api/v1/timestamp"):
                    return FakeResponse({"code": "500000", "msg": "boom"})
                return FakeResponse({"code": "200000", "data": []})

        session = BrokenClockSession()
        self.make_client(session).accounts()  # must not raise
        sent = int(session.calls[-1]["headers"]["KC-API-TIMESTAMP"])
        self.assertLess(abs(sent - int(time.time() * 1000)), 5_000)

    def test_rejected_timestamp_triggers_one_resync_and_retry(self):
        class SkewRejectingSession(self.ClockSession):
            def __init__(self):
                super().__init__()
                self.account_calls = 0

            def request(self, method, url, data=None, headers=None, timeout=None):
                self.calls.append({"method": method, "url": url, "headers": headers})
                if url.endswith("/api/v1/timestamp"):
                    return FakeResponse({"code": "200000", "data": int(time.time() * 1000)})
                self.account_calls += 1
                if self.account_calls == 1:
                    return FakeResponse({"code": "400002", "msg": "KC-API-TIMESTAMP Invalid"})
                return FakeResponse({"code": "200000", "data": ["ok"]})

        session = SkewRejectingSession()
        self.assertEqual(self.make_client(session).accounts(), ["ok"])
        self.assertEqual(session.account_calls, 2)

    def test_persistent_skew_rejection_eventually_raises(self):
        class AlwaysSkewedSession(self.ClockSession):
            def request(self, method, url, data=None, headers=None, timeout=None):
                self.calls.append({"method": method, "url": url, "headers": headers})
                if url.endswith("/api/v1/timestamp"):
                    return FakeResponse({"code": "200000", "data": int(time.time() * 1000)})
                return FakeResponse({"code": "400002", "msg": "KC-API-TIMESTAMP Invalid"})

        with self.assertRaises(KuCoinAPIError) as ctx:
            self.make_client(AlwaysSkewedSession()).accounts()
        self.assertEqual(ctx.exception.code, "400002")


class RetryTests(unittest.TestCase):
    """Rate limits and transient failures must not kill a long-running loop."""

    def make_client(self, session, **kwargs):
        return KuCoinClient(
            api_key="k",
            api_secret="s",
            api_passphrase="p",
            session=session,
            sync_clock=False,
            retry_backoff=0,  # keep the suite fast
            **kwargs,
        )

    def test_rate_limited_get_is_retried_then_succeeds(self):
        class ThrottledSession:
            def __init__(self):
                self.calls = 0

            def request(self, method, url, data=None, headers=None, timeout=None):
                self.calls += 1
                if self.calls < 3:
                    return FakeResponse({"code": "429000", "msg": "Too Many Requests"})
                return FakeResponse({"code": "200000", "data": "ok"})

        session = ThrottledSession()
        self.assertEqual(self.make_client(session).server_time(), "ok")
        self.assertEqual(session.calls, 3)

    def test_rate_limit_gives_up_after_max_retries(self):
        class AlwaysThrottled:
            def __init__(self):
                self.calls = 0

            def request(self, method, url, data=None, headers=None, timeout=None):
                self.calls += 1
                return FakeResponse({"code": "429000", "msg": "Too Many Requests"})

        session = AlwaysThrottled()
        with self.assertRaises(KuCoinAPIError):
            self.make_client(session, max_retries=2).server_time()
        self.assertEqual(session.calls, 3)  # initial attempt plus two retries

    def test_rate_limited_post_is_retried(self):
        # A throttled request was refused before executing, so replaying an
        # order POST cannot double-submit.
        class ThrottledOnce:
            def __init__(self):
                self.calls = 0

            def request(self, method, url, data=None, headers=None, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    return FakeResponse({"code": "429000", "msg": "Too Many Requests"})
                return FakeResponse({"code": "200000", "data": {"orderId": "x"}})

        session = ThrottledOnce()
        self.assertEqual(self.make_client(session).create_order({}), {"orderId": "x"})
        self.assertEqual(session.calls, 2)

    def test_network_error_on_get_is_retried(self):
        class FlakySession:
            def __init__(self):
                self.calls = 0

            def request(self, method, url, data=None, headers=None, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise requests.ConnectionError("reset")
                return FakeResponse({"code": "200000", "data": "ok"})

        session = FlakySession()
        self.assertEqual(self.make_client(session).server_time(), "ok")
        self.assertEqual(session.calls, 2)

    def test_network_error_on_post_is_never_retried(self):
        # The order may already have reached KuCoin, so a replay risks a
        # duplicate submission. This is the important one.
        class DroppedSession:
            def __init__(self):
                self.calls = 0

            def request(self, method, url, data=None, headers=None, timeout=None):
                self.calls += 1
                raise requests.Timeout("no response")

        session = DroppedSession()
        with self.assertRaises(requests.Timeout):
            self.make_client(session).create_order({"clientOid": "abc"})
        self.assertEqual(session.calls, 1)

    def test_server_error_on_post_is_not_retried(self):
        class ServerErrorSession:
            def __init__(self):
                self.calls = 0

            def request(self, method, url, data=None, headers=None, timeout=None):
                self.calls += 1
                resp = FakeResponse({}, status_code=502)
                resp.json = lambda: (_ for _ in ()).throw(ValueError("not json"))
                resp.raise_for_status = lambda: (_ for _ in ()).throw(
                    requests.HTTPError("502 Bad Gateway")
                )
                return resp

        session = ServerErrorSession()
        with self.assertRaises(requests.HTTPError):
            self.make_client(session).create_order({})
        self.assertEqual(session.calls, 1)

    def test_business_error_is_not_retried(self):
        class InvalidSymbolSession:
            def __init__(self):
                self.calls = 0

            def request(self, method, url, data=None, headers=None, timeout=None):
                self.calls += 1
                return FakeResponse({"code": "400100", "msg": "Invalid symbol"})

        session = InvalidSymbolSession()
        with self.assertRaises(KuCoinAPIError):
            self.make_client(session).server_time()
        self.assertEqual(session.calls, 1)


class OrderSafetyTests(unittest.TestCase):
    def test_limit_order_requires_confirm(self):
        client = FakeOrderClient()
        with self.assertRaises(OrderRejected):
            place_limit_order(client, "BTC-USDT", "buy", size="0.001", price="50000")
        self.assertEqual(client.orders, [])

    def test_market_order_requires_confirm(self):
        client = FakeOrderClient()
        with self.assertRaises(OrderRejected):
            place_market_order(client, "BTC-USDT", "sell", size="0.001")
        self.assertEqual(client.orders, [])

    def test_confirmed_limit_order_is_submitted(self):
        client = FakeOrderClient()
        result = place_limit_order(
            client, "BTC-USDT", "BUY", size="0.001", price="50000", confirm=True
        )
        self.assertEqual(result["orderId"], "fake-order-id")
        payload = client.orders[0]
        self.assertEqual(payload["side"], "buy")
        self.assertEqual(payload["type"], "limit")
        self.assertEqual(payload["price"], "50000")
        self.assertEqual(payload["size"], "0.001")
        self.assertTrue(payload["clientOid"])

    def test_invalid_side_rejected(self):
        with self.assertRaises(OrderRejected):
            place_limit_order(FakeOrderClient(), "BTC-USDT", "hold", "0.001", "50000", confirm=True)

    def test_invalid_symbol_rejected(self):
        with self.assertRaises(OrderRejected):
            place_limit_order(FakeOrderClient(), "BTCUSDT", "buy", "0.001", "50000", confirm=True)

    def test_non_positive_size_rejected(self):
        with self.assertRaises(OrderRejected):
            place_limit_order(FakeOrderClient(), "BTC-USDT", "buy", "0", "50000", confirm=True)

    def test_market_order_needs_exactly_one_of_size_or_funds(self):
        with self.assertRaises(OrderRejected):
            place_market_order(FakeOrderClient(), "BTC-USDT", "buy", confirm=True)
        with self.assertRaises(OrderRejected):
            place_market_order(
                FakeOrderClient(), "BTC-USDT", "buy", size="0.001", funds="100", confirm=True
            )

    def test_market_order_with_funds(self):
        client = FakeOrderClient()
        place_market_order(client, "BTC-USDT", "buy", funds="100", confirm=True)
        self.assertEqual(client.orders[0]["funds"], "100")
        self.assertNotIn("size", client.orders[0])

    def test_cancel_requires_order_id(self):
        with self.assertRaises(OrderRejected):
            cancel_order(FakeOrderClient(), "")

    def test_cancel_requires_confirm(self):
        class RecordingClient(FakeOrderClient):
            def __init__(self):
                super().__init__()
                self.cancelled = []

            def cancel_order(self, order_id):
                self.cancelled.append(order_id)
                return {"cancelledOrderIds": [order_id]}

        client = RecordingClient()
        with self.assertRaises(OrderRejected):
            cancel_order(client, "order-1")
        self.assertEqual(client.cancelled, [])

    def test_confirmed_cancel_is_submitted(self):
        result = cancel_order(FakeOrderClient(), "order-1", confirm=True)
        self.assertEqual(result["cancelledOrderIds"], ["order-1"])


class NumericFormattingTests(unittest.TestCase):
    """Sizes and prices must reach KuCoin as plain decimals, never 1e-05."""

    def submit(self, **kwargs):
        client = FakeOrderClient()
        place_limit_order(client, "BTC-USDT", "buy", confirm=True, **kwargs)
        return client.orders[0]

    def test_small_float_size_is_not_scientific_notation(self):
        payload = self.submit(size=0.00001, price=50000)
        self.assertEqual(payload["size"], "0.00001")
        self.assertEqual(payload["price"], "50000")

    def test_large_float_price_is_not_scientific_notation(self):
        self.assertEqual(self.submit(size="1", price=1e17)["price"], "100000000000000000")

    def test_scientific_notation_string_is_normalized(self):
        self.assertEqual(self.submit(size="1e-5", price="50000")["size"], "0.00001")

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(self.submit(size=" 0.001 ", price="50000")["size"], "0.001")

    def test_decimal_string_passes_through_unchanged(self):
        self.assertEqual(self.submit(size="0.001", price="50000")["size"], "0.001")

    def test_nan_size_rejected(self):
        client = FakeOrderClient()
        with self.assertRaises(OrderRejected):
            place_limit_order(
                client, "BTC-USDT", "buy", size=float("nan"), price="50000", confirm=True
            )
        self.assertEqual(client.orders, [])

    def test_infinite_price_rejected(self):
        with self.assertRaises(OrderRejected):
            place_limit_order(
                FakeOrderClient(), "BTC-USDT", "buy", size="1", price=float("inf"), confirm=True
            )

    def test_non_numeric_values_rejected(self):
        for bad in (True, None, "abc", ""):
            with self.subTest(value=bad):
                with self.assertRaises(OrderRejected):
                    place_limit_order(
                        FakeOrderClient(), "BTC-USDT", "buy", size=bad, price="50000", confirm=True
                    )

    def test_market_order_funds_formatted(self):
        client = FakeOrderClient()
        place_market_order(client, "BTC-USDT", "buy", funds=0.00001, confirm=True)
        self.assertEqual(client.orders[0]["funds"], "0.00001")


class OpenOrdersPaginationTests(unittest.TestCase):
    class PagedClient:
        """Serves a paginated /api/v1/orders envelope, 50 items per page."""

        def __init__(self, total):
            self.total = total
            self.requested_pages = []

        def list_orders(self, status="active", symbol=None, current_page=None, page_size=None):
            self.requested_pages.append(current_page)
            size = 50  # server caps below the requested page_size
            start = (current_page - 1) * size
            items = [{"id": f"o{i}"} for i in range(start, min(start + size, self.total))]
            return {
                "currentPage": current_page,
                "pageSize": size,
                "totalNum": self.total,
                "totalPage": -(-self.total // size),
                "items": items,
            }

    def test_all_pages_are_followed(self):
        client = self.PagedClient(total=137)
        orders = open_orders(client)
        self.assertEqual(len(orders), 137)
        self.assertEqual(client.requested_pages, [1, 2, 3])
        self.assertEqual(orders[-1]["id"], "o136")

    def test_single_page_makes_one_request(self):
        client = self.PagedClient(total=3)
        self.assertEqual(len(open_orders(client)), 3)
        self.assertEqual(client.requested_pages, [1])

    def test_no_open_orders(self):
        client = self.PagedClient(total=0)
        self.assertEqual(open_orders(client), [])

    def test_stops_when_server_overstates_total_pages(self):
        class LyingClient:
            def __init__(self):
                self.calls = 0

            def list_orders(self, status="active", symbol=None, current_page=None, page_size=None):
                self.calls += 1
                return {"totalPage": 99, "items": [] if current_page > 1 else [{"id": "o0"}]}

        client = LyingClient()
        self.assertEqual(len(open_orders(client)), 1)
        self.assertEqual(client.calls, 2)

    def test_bare_list_response_is_tolerated(self):
        class ListClient:
            def list_orders(self, status="active", symbol=None, current_page=None, page_size=None):
                return [{"id": "o0"}]

        self.assertEqual(open_orders(ListClient()), [{"id": "o0"}])


class CandleParsingTests(unittest.TestCase):
    def test_candles_parsed_and_sorted_oldest_first(self):
        class CandleClient:
            def candles(self, symbol, interval="1hour", start_at=None, end_at=None):
                # KuCoin returns newest first, values as strings
                return [
                    ["1700003600", "101", "102", "103", "100", "5", "500"],
                    ["1700000000", "99", "101", "102", "98", "4", "400"],
                ]

        candles = recent_candles(CandleClient(), "BTC-USDT")
        self.assertEqual([c["time"] for c in candles], [1700000000, 1700003600])
        self.assertEqual(candles[0]["open"], 99.0)
        self.assertEqual(candles[1]["close"], 102.0)

    def test_null_data_returns_empty_list(self):
        class EmptyRangeClient:
            def candles(self, symbol, interval="1hour", start_at=None, end_at=None):
                return None  # KuCoin sends data: null for a range with no candles

        self.assertEqual(recent_candles(EmptyRangeClient(), "BTC-USDT"), [])


if __name__ == "__main__":
    unittest.main()
