"""Kraken desk: READ-ONLY market data and account reporting for Kraken.

This package can read Kraken's public market data (ticker, daily OHLC) and,
with an API key, the account's balances and open orders. It cannot place,
modify or cancel orders, withdraw, or move funds, and that is enforced in
code: `kraken.client` allowlists the endpoints it will call, and every
trading/funding endpoint sits outside that list.

Two layers, deliberately:

1. **Server side (the real guarantee).** Create the Kraken API key with
   ONLY "Query Funds" and "Query Open Orders & Trades". A key without
   trade/withdraw permission cannot trade no matter what code holds it.
2. **Client side (this package).** `READ_ONLY_PRIVATE` is a frozen
   allowlist, locked by tests/test_kraken.py, so a careless edit can't turn
   the desk into an order router.

Stdlib only for the client (urllib + hmac), pandas for the bar frames. Like
`ibkr.cache`, nothing here imports a broker library.
"""
