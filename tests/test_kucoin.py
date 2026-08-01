import json
import time
import unittest

import requests

from kucoin.client import KuCoinAPIError, KuCoinClient, MissingCredentials
from kucoin.data import recent_candles
from kucoin.orders import (
    ORDER_PAGE_SIZE,
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
        client.candles("BTC-USDT", type="1hour", start_at=None, end_at=None)
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
            auto_sync_time=False,
        )
        client.accounts(currency="USDT")
        headers = session.calls[-1]["headers"]
        self.assertIn("KC-API-SIGN", headers)
        self.assertEqual(headers["KC-API-KEY"], "test-key")
        self.assertTrue(session.calls[-1]["url"].endswith("/api/v1/accounts?currency=USDT"))


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


class NumberFormattingTests(unittest.TestCase):
    """KuCoin rejects scientific notation, NaN and Infinity."""

    def submitted(self, **kwargs):
        client = FakeOrderClient()
        place_limit_order(client, "BTC-USDT", "buy", confirm=True, **kwargs)
        return client.orders[0]

    def test_small_size_is_not_scientific_notation(self):
        payload = self.submitted(size=0.00001, price=50000)
        self.assertEqual(payload["size"], "0.00001")
        self.assertEqual(payload["price"], "50000")

    def test_very_small_size_is_not_scientific_notation(self):
        self.assertEqual(self.submitted(size=1e-9, price="50000")["size"], "0.000000001")

    def test_string_inputs_survive_unchanged(self):
        payload = self.submitted(size="0.001", price="50000.5")
        self.assertEqual(payload["size"], "0.001")
        self.assertEqual(payload["price"], "50000.5")

    def test_nan_is_rejected(self):
        for bad in ("nan", float("nan")):
            with self.assertRaises(OrderRejected):
                self.submitted(size=bad, price="50000")

    def test_infinity_is_rejected(self):
        for bad in ("inf", float("inf"), float("-inf")):
            with self.assertRaises(OrderRejected):
                self.submitted(size=bad, price="50000")

    def test_non_numeric_still_rejected(self):
        with self.assertRaises(OrderRejected):
            self.submitted(size="abc", price="50000")


class OpenOrdersPaginationTests(unittest.TestCase):
    class PagedClient:
        def __init__(self, total, total_page_field=True):
            self.total = total
            self.total_page_field = total_page_field
            self.pages_requested = []

        def list_orders(self, status="active", symbol=None, current_page=None, page_size=None):
            self.pages_requested.append(current_page)
            start = (current_page - 1) * page_size
            items = [{"id": i} for i in range(start, min(start + page_size, self.total))]
            page = {"currentPage": current_page, "pageSize": page_size, "items": items}
            if self.total_page_field:
                page["totalPage"] = -(-self.total // page_size) or 1
            return page

    def test_all_pages_are_followed(self):
        client = self.PagedClient(total=ORDER_PAGE_SIZE * 2 + 7)
        orders = open_orders(client)
        self.assertEqual(len(orders), ORDER_PAGE_SIZE * 2 + 7)
        self.assertEqual(client.pages_requested, [1, 2, 3])

    def test_single_page_makes_one_request(self):
        client = self.PagedClient(total=3)
        self.assertEqual(len(open_orders(client)), 3)
        self.assertEqual(client.pages_requested, [1])

    def test_missing_total_page_falls_back_to_short_page(self):
        client = self.PagedClient(total=ORDER_PAGE_SIZE + 2, total_page_field=False)
        self.assertEqual(len(open_orders(client)), ORDER_PAGE_SIZE + 2)
        self.assertEqual(client.pages_requested, [1, 2])

    def test_bare_list_response_is_returned_as_is(self):
        class BareClient:
            def list_orders(self, **kwargs):
                return [{"id": 1}]

        self.assertEqual(open_orders(BareClient()), [{"id": 1}])


class ClockSyncTests(unittest.TestCase):
    def make_client(self, session, **kwargs):
        return KuCoinClient(
            api_key="k", api_secret="s", api_passphrase="p", session=session, **kwargs
        )

    def test_offset_is_applied_to_signed_timestamp(self):
        class TimeSession(FakeSession):
            def request(self, method, url, data=None, headers=None, timeout=None):
                self.calls.append({"url": url, "headers": headers})
                if url.endswith("/api/v1/timestamp"):
                    # Server clock is an hour ahead of ours.
                    return FakeResponse(
                        {"code": "200000", "data": int(time.time() * 1000) + 3_600_000}
                    )
                return FakeResponse({"code": "200000", "data": []})

        session = TimeSession()
        client = self.make_client(session)
        client.accounts()
        offset = client._time_offset_ms
        self.assertGreater(offset, 3_500_000)
        signed_ts = int(session.calls[-1]["headers"]["KC-API-TIMESTAMP"])
        self.assertGreater(signed_ts, int(time.time() * 1000) + 3_500_000)

    def test_sync_is_attempted_only_once(self):
        session = FakeSession(data=[])
        client = self.make_client(session)
        client.accounts()
        client.accounts()
        probes = [c for c in session.calls if c["url"].endswith("/api/v1/timestamp")]
        self.assertEqual(len(probes), 1)

    def test_failed_sync_does_not_break_signing(self):
        class BrokenTimeSession(FakeSession):
            def request(self, method, url, data=None, headers=None, timeout=None):
                self.calls.append({"url": url, "headers": headers})
                if url.endswith("/api/v1/timestamp"):
                    return FakeResponse({"code": "500000", "msg": "boom"})
                return FakeResponse({"code": "200000", "data": []})

        session = BrokenTimeSession()
        client = self.make_client(session)
        self.assertEqual(client.accounts(), [])
        self.assertEqual(client._time_offset_ms, 0)

    def test_auto_sync_can_be_disabled(self):
        session = FakeSession(data=[])
        client = self.make_client(session, auto_sync_time=False)
        client.accounts()
        self.assertEqual(len(session.calls), 1)


class RetryTests(unittest.TestCase):
    def setUp(self):
        # Keep retry tests instant.
        self._sleeps = []
        self._real_sleep = time.sleep
        time.sleep = self._sleeps.append
        self.addCleanup(lambda: setattr(time, "sleep", self._real_sleep))

    def make_client(self, session, **kwargs):
        return KuCoinClient(
            api_key="", api_secret="", api_passphrase="", session=session, **kwargs
        )

    def test_429_is_retried_then_succeeds(self):
        class Flaky(FakeSession):
            def __init__(self):
                super().__init__()
                self.n = 0

            def request(self, *args, **kwargs):
                self.n += 1
                if self.n < 3:
                    return FakeResponse({"code": "200000", "data": None}, status_code=429)
                return FakeResponse({"code": "200000", "data": 42})

        session = Flaky()
        self.assertEqual(self.make_client(session).server_time(), 42)
        self.assertEqual(session.n, 3)
        self.assertEqual(self._sleeps, [0.5, 1.0])

    def test_in_band_rate_limit_code_is_retried(self):
        class Limited(FakeSession):
            def __init__(self):
                super().__init__()
                self.n = 0

            def request(self, *args, **kwargs):
                self.n += 1
                if self.n < 2:
                    return FakeResponse({"code": "429000", "msg": "Too Many Requests"})
                return FakeResponse({"code": "200000", "data": 7})

        self.assertEqual(self.make_client(Limited()).server_time(), 7)

    def test_retry_after_header_is_honoured(self):
        class WithHeader(FakeSession):
            def __init__(self):
                super().__init__()
                self.n = 0

            def request(self, *args, **kwargs):
                self.n += 1
                if self.n == 1:
                    resp = FakeResponse({"code": "200000", "data": None}, status_code=503)
                    resp.headers = {"Retry-After": "2.5"}
                    return resp
                return FakeResponse({"code": "200000", "data": 1})

        self.make_client(WithHeader()).server_time()
        self.assertEqual(self._sleeps, [2.5])

    def test_retries_are_exhausted_then_error_raised(self):
        class AlwaysLimited(FakeSession):
            def request(self, *args, **kwargs):
                return FakeResponse({"code": "429000", "msg": "Too Many Requests"}, status_code=200)

        with self.assertRaises(KuCoinAPIError) as ctx:
            self.make_client(AlwaysLimited()).server_time()
        self.assertEqual(ctx.exception.code, "429000")

    def test_connection_error_is_retried(self):
        class Dropping(FakeSession):
            def __init__(self):
                super().__init__()
                self.n = 0

            def request(self, *args, **kwargs):
                self.n += 1
                if self.n < 3:
                    raise requests.ConnectionError("reset")
                return FakeResponse({"code": "200000", "data": 5})

        self.assertEqual(self.make_client(Dropping()).server_time(), 5)

    def test_connection_error_propagates_after_retries(self):
        class Dead(FakeSession):
            def request(self, *args, **kwargs):
                raise requests.ConnectionError("reset")

        with self.assertRaises(requests.ConnectionError):
            self.make_client(Dead()).server_time()

    def test_client_error_is_not_retried(self):
        class BadRequest(FakeSession):
            def __init__(self):
                super().__init__()
                self.n = 0

            def request(self, *args, **kwargs):
                self.n += 1
                return FakeResponse({"code": "400100", "msg": "Invalid symbol"}, status_code=400)

        session = BadRequest()
        with self.assertRaises(KuCoinAPIError):
            self.make_client(session).server_time()
        self.assertEqual(session.n, 1)


class ResponseParsingTests(unittest.TestCase):
    def make_client(self, session):
        return KuCoinClient(api_key="", api_secret="", api_passphrase="", session=session)

    def test_non_dict_json_raises_api_error(self):
        class ListSession(FakeSession):
            def request(self, *args, **kwargs):
                return FakeResponse(["not", "an", "envelope"])

        with self.assertRaises(KuCoinAPIError) as ctx:
            self.make_client(ListSession()).server_time()
        self.assertIn("unexpected response body", ctx.exception.message)

    def test_non_json_body_raises_api_error(self):
        class HtmlResponse:
            status_code = 200
            text = "<html>maintenance</html>"

            def json(self):
                raise ValueError("no json")

            def raise_for_status(self):
                pass

        class HtmlSession(FakeSession):
            def request(self, *args, **kwargs):
                return HtmlResponse()

        with self.assertRaises(KuCoinAPIError):
            self.make_client(HtmlSession()).server_time()


class CandleParsingTests(unittest.TestCase):
    def test_candles_parsed_and_sorted_oldest_first(self):
        class CandleClient:
            def candles(self, symbol, type="1hour", start_at=None, end_at=None):
                # KuCoin returns newest first, values as strings
                return [
                    ["1700003600", "101", "102", "103", "100", "5", "500"],
                    ["1700000000", "99", "101", "102", "98", "4", "400"],
                ]

        candles = recent_candles(CandleClient(), "BTC-USDT")
        self.assertEqual([c["time"] for c in candles], [1700000000, 1700003600])
        self.assertEqual(candles[0]["open"], 99.0)
        self.assertEqual(candles[1]["close"], 102.0)


if __name__ == "__main__":
    unittest.main()
