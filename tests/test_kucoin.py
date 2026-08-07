import json
import unittest

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
        )
        client.accounts(currency="USDT")
        headers = session.calls[0]["headers"]
        self.assertIn("KC-API-SIGN", headers)
        self.assertEqual(headers["KC-API-KEY"], "test-key")
        self.assertTrue(session.calls[0]["url"].endswith("/api/v1/accounts?currency=USDT"))


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

    def test_null_data_returns_empty_list(self):
        class EmptyRangeClient:
            def candles(self, symbol, type="1hour", start_at=None, end_at=None):
                return None  # KuCoin sends data: null for a range with no candles

        self.assertEqual(recent_candles(EmptyRangeClient(), "BTC-USDT"), [])


if __name__ == "__main__":
    unittest.main()
