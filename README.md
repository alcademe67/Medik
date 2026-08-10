# Medik

## Interactive Brokers TWS connection

Python connects to a locally running TWS (Trader Workstation) instance over its
socket API, using the [`ib_async`](https://github.com/ib-api-reloaded/ib_async)
library.

### 1. Configure TWS

In TWS, go to **File > Global Configuration > API > Settings** and set:

- **Enable ActiveX and Socket Clients** — checked
- **Socket port** — `7496` (TWS Live Trading; `7497` is Paper Trading)
- **Trusted IPs** — add `127.0.0.1`
- **Read-Only API** — unchecked, if you want to place orders from Python; checked
  if you only want read access

Leave TWS open and logged into the account you want to connect to — the API
only works while TWS itself is running.

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure connection settings

```bash
cp .env.example .env
```

Defaults are `127.0.0.1:7496` (TWS Live). Edit `.env` if your Global
Configuration uses a different port or you're running multiple TWS/Gateway
instances (each needs a distinct `IBKR_CLIENT_ID`).

### 4. Test the connection

```bash
python examples/connect_test.py
```

This prints account summary and current positions — read-only, no orders.

### 5. Placing orders

`ibkr/orders.py` has `place_limit_order` / `place_market_order` helpers. Both
require an explicit `confirm=True` — without it they raise `OrderRejected`
instead of submitting anything, so a script can't send a live order by
accident. See `examples/place_order_example.py`.

This is a **live trading account** — orders placed this way use real money.
Prefer limit orders over market orders, and double check symbol/side/quantity/
price before setting `confirm=True`.