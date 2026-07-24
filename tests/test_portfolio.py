import unittest

from kucoin.portfolio import aggregate_by_currency, summarize, value_in_usdt

ACCOUNTS = [
    {"currency": "USDT", "type": "trade", "balance": "100.5", "available": "100.5", "holds": "0"},
    {"currency": "USDT", "type": "main", "balance": "49.5", "available": "40.0", "holds": "9.5"},
    {"currency": "BTC", "type": "trade", "balance": "0.01", "available": "0.01", "holds": "0"},
    {"currency": "XYZ", "type": "trade", "balance": "5", "available": "5", "holds": "0"},
    {"currency": "ETH", "type": "main", "balance": "0", "available": "0", "holds": "0"},
]


def price_lookup(currency):
    # BTC is priced; XYZ has no USDT market.
    return {"BTC": 65000.0, "XYZ": None}.get(currency)


class AggregateTests(unittest.TestCase):
    def test_sums_across_account_types_and_skips_zero(self):
        totals = aggregate_by_currency(ACCOUNTS)
        self.assertNotIn("ETH", totals)  # zero balance dropped
        self.assertAlmostEqual(totals["USDT"]["balance"], 150.0)
        self.assertAlmostEqual(totals["USDT"]["available"], 140.5)
        self.assertAlmostEqual(totals["USDT"]["holds"], 9.5)
        self.assertAlmostEqual(totals["BTC"]["balance"], 0.01)


class ValueTests(unittest.TestCase):
    def test_stablecoin_valued_one_to_one(self):
        self.assertEqual(value_in_usdt("USDT", 150.0, price_lookup), 150.0)

    def test_priced_asset_multiplied(self):
        self.assertEqual(value_in_usdt("BTC", 0.01, price_lookup), 650.0)

    def test_unpriced_asset_returns_none(self):
        self.assertIsNone(value_in_usdt("XYZ", 5.0, price_lookup))


class SummarizeTests(unittest.TestCase):
    def test_total_excludes_unpriced_and_sorts_richest_first(self):
        summary = summarize(ACCOUNTS, price_lookup)
        # 150 USDT + 0.01 BTC * 65000 = 800; XYZ excluded
        self.assertAlmostEqual(summary["total_usdt"], 800.0)
        self.assertEqual(summary["unpriced"], ["XYZ"])
        # richest first: USDT (150) then BTC (650)? BTC value 650 > USDT 150
        self.assertEqual(summary["rows"][0]["currency"], "BTC")
        self.assertEqual(summary["rows"][1]["currency"], "USDT")
        # unpriced row sorts last with None value
        self.assertEqual(summary["rows"][-1]["currency"], "XYZ")
        self.assertIsNone(summary["rows"][-1]["usdt_value"])

    def test_empty_accounts(self):
        summary = summarize([], price_lookup)
        self.assertEqual(summary["rows"], [])
        self.assertEqual(summary["total_usdt"], 0.0)
        self.assertEqual(summary["unpriced"], [])


if __name__ == "__main__":
    unittest.main()
