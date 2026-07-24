# Medik

KuCoin spot-trading automation toolkit: a minimal signed REST client,
safety-gated order helpers, market-data utilities, and runnable examples.

## Layout

- `kucoin/client.py` - `KuCoinClient`, a thin REST wrapper implementing
  KuCoin API key version 2 request signing. Public market data works with
  no credentials; private endpoints read keys from the environment.
- `kucoin/orders.py` - `place_limit_order`, `place_market_order`,
  `cancel_order`, `open_orders`. Every order-placing call refuses to
  submit unless `confirm=True` is passed explicitly.
- `kucoin/data.py` - ticker and candle helpers (`last_price`,
  `best_bid_ask`, `recent_candles`).
- `kucoin/portfolio.py` - pure balance-summary helpers (`aggregate_by_currency`,
  `value_in_usdt`, `summarize`) used to total a portfolio in USDT.
- `examples/` - read-only connectivity test, a balance summary, a price
  monitor loop, and a dry-run-by-default order example.
- `tests/` - offline unit tests (signing vectors, order safety gates,
  candle parsing). No network required.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your KuCoin API key/secret/passphrase
```

API keys are created at <https://www.kucoin.com/account/api>. Grant only
General + Spot Trading permissions - nothing in this project needs
withdrawal access. `.env` is gitignored so credentials stay local.

## Usage

Run everything from the repository root:

```bash
# Read-only smoke test: server time, BTC-USDT ticker, balances (if keys set)
python -m examples.kucoin_connect_test

# Balance summary: non-zero holdings plus an estimated total USDT value
python -m examples.kucoin_balance

# Poll a ticker and announce threshold crossings (read-only)
python -m examples.kucoin_price_monitor --symbol BTC-USDT --above 120000 --below 100000

# Order example: prints what it would do; only submits with LIVE=1
python -m examples.kucoin_place_order_example
LIVE=1 python -m examples.kucoin_place_order_example
```

Programmatic use:

```python
from kucoin.client import KuCoinClient
from kucoin.data import last_price
from kucoin.orders import place_limit_order

client = KuCoinClient()  # reads KUCOIN_* env vars
print(last_price(client, "BTC-USDT"))
place_limit_order(client, "BTC-USDT", "buy", size="0.001", price="50000", confirm=True)
```

## Safety model

- No order is ever sent without an explicit `confirm=True`; omitting it
  raises `OrderRejected` before any network call.
- The order example is a dry run unless the `LIVE=1` environment variable
  is set.
- Inputs (side, symbol shape, positive size/price, size-vs-funds) are
  validated locally before submission.

## Tests

```bash
python -m unittest
```
