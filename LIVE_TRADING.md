# Live trading — setup, safety, and deployment

This document covers the live-trading engine added to the `bot/` package.
Read it fully before enabling live trading. **Automated trading can lose
money quickly; nothing here is financial advice.**

## The safety model (how the bot protects you)

Live orders are impossible unless **every** one of these holds:

1. `LIVE_TRADING=true` in `.env`
2. Valid KuCoin API keys (auth succeeds at startup)
3. You ran `python -m bot.golive` and confirmed the checklist — this writes
   a go-live token file the engine requires
4. All startup **health checks** pass (reachable, keys valid, symbol tradable)

Miss any one and the engine runs in **paper mode** (simulated fills, no real
orders). Two independent switches (the env flag *and* the token) mean a
stray config change can't silently start trading your money.

Additional guarantees built into the code:
- `place_market_order` refuses to touch the network unless `LIVE_TRADING=true`.
- The bot **never** calls any withdrawal or transfer endpoint — it cannot
  move funds off the exchange even if the key allowed it.
- Secrets are read only from `.env`, never hardcoded, never logged.

## Before every order

`bot/execution.py` validates each buy and rejects it (no order sent) unless:
the symbol is tradable, size ≥ the exchange minimum, order value ≥ the floor,
you hold enough quote currency, there's no duplicate position or resting
order, and a minimum interval since the last order has passed.

## Risk controls (`bot/risk.py`, all configurable in `.env`)

| Control | Default | Env var |
|---------|---------|---------|
| Risk per trade (to the stop) | 1% of equity | `MAX_RISK_PER_TRADE` |
| **Max position size (notional)** | **25% of equity** | **`MAX_POSITION_PCT`** |
| Max open positions | 3 | `MAX_OPEN_POSITIONS` |
| Daily loss limit | 5% | `DAILY_LOSS_LIMIT_PCT` |
| **Daily order cap** | **10 entries/day** | **`MAX_DAILY_ORDERS`** |
| Pause after N losses | 3 | `MAX_CONSECUTIVE_LOSSES` |
| Stop-loss | 2×ATR | `STOP_ATR_MULT` |
| Take-profit | 2:1 | `TAKE_PROFIT_RR` |
| Breakeven stop | off | `BREAKEVEN_TRIGGER_PCT` (>0 moves stop to entry once up X%) |
| ATR trailing stop | off | `TRAILING_ATR_MULT` (>0 trails this×ATR below the high) |

Stops **only ever move up** (toward locking in profit), never down. Every
stop adjustment is logged and sent to your notification channel.

**Why two size controls?** `MAX_RISK_PER_TRADE` caps the *loss to the stop*
(1% of equity). But with a tight stop that can still deploy most of the
account into one trade. `MAX_POSITION_PCT` is a second, independent ceiling
on the *notional* — no single position may use more than this % of equity,
whatever the stop distance. For your first live runs, set it small (10–20).

**Order & fee audit trail.** Every live BUY/SELL logs, in `logs/bot.log`, the
intended order *and* — fetched back from KuCoin — the actual filled size,
volume-weighted average fill price, and fee paid (`FILL BUY … fee=… USDT`).
Errors are logged too; a fills-lookup failure is logged but never interrupts
trading (the order already executed).

**Emergency stop:** create a file named `STOP` in the working directory
(default; set `EMERGENCY_STOP_PATH`) and the engine halts trading and shuts
down gracefully on its next tick — no terminal access needed
(`docker exec <container> touch STOP` works too). Delete the file before
restarting. A **daily summary** (realized P/L + all-time stats) is sent to
your notification channel at each UTC-day rollover.

## Recommended path to live (do not skip)

1. **Backtest** a strategy until it shows an edge: `python -m trading backtest-top 1h`.
2. **Paper trade** it: run the engine with `LIVE_TRADING=false` for weeks and
   watch the Telegram alerts and dashboard.
3. **Create a KuCoin key** with **Trade** permission and **no** withdrawal
   permission. Put it in `.env`.
4. **Arm it**: `python -m bot.golive` — pass the health checks, answer the
   attestations honestly, type the confirmation phrase.
5. Set `LIVE_TRADING=true`, start small (`MAX_RISK_PER_TRADE=0.005` or less).

### Exact `.env` for a small first live trade

The only lines you must add/change to go live are marked ★. Everything else
has a safe default; the values below just make the first trade deliberately
small. Copy into `.env`:

```dotenv
# ★ 1. Master switch (still needs the go-live token below)
LIVE_TRADING=true

# ★ 2. Your KuCoin key WITH Trade permission, WITHOUT withdrawal permission
KUCOIN_API_KEY=your_key
KUCOIN_API_SECRET=your_secret
KUCOIN_API_PASSPHRASE=your_passphrase
KUCOIN_KEY_VERSION=2

# 3. What to trade, kept small
TRADE_SYMBOL=BTC-USDT
QUOTE_CURRENCY=USDT

# 4. Deliberately conservative risk for the first live runs
MAX_RISK_PER_TRADE=0.005   # risk 0.5% of equity to the stop
MAX_POSITION_PCT=10        # never deploy more than 10% of the account per trade
MAX_OPEN_POSITIONS=1       # one position at a time while you watch it
DAILY_LOSS_LIMIT_PCT=3     # stop for the day after a 3% account loss
MAX_DAILY_ORDERS=5         # at most 5 entries a day
MAX_CONSECUTIVE_LOSSES=2   # pause after 2 losers in a row

# 5. Phone alerts (recommended so you see every fill)
NTFY_TOPIC=your-unique-topic
```

Then, in order:

```bash
python -m bot.preflight     # read-only: confirms keys + shows the live gate
python -m bot.golive        # arm: health checks, attestations, type the phrase
python -m bot.live_engine   # start trading (announces LIVE 💸 on startup)
```

`preflight` and `golive` place **no** orders. The engine prints and notifies
`LIVE 💸` vs `PAPER` on startup — check that line before walking away.

To disarm at any time: delete the go-live token file (`.golive_confirmed`)
or set `LIVE_TRADING=false`.

## Running it

```bash
pip install -r requirements.txt

python -m bot.live_engine       # the trading engine (paper unless armed)
uvicorn bot.dashboard:app --host 0.0.0.0 --port 8000   # dashboard at :8000
python -m bot.golive            # arm live trading (interactive checklist)
```

The engine recovers open positions from SQLite after a restart or crash.

## Docker / cloud deployment

```bash
cp .env.example .env            # fill in keys; keep LIVE_TRADING=false at first
docker compose up -d            # engine + dashboard, auto-restart on reboot/crash
docker compose logs -f engine
```

`restart: unless-stopped` brings both services back after a crash or server
reboot. State and the go-live token persist in `./data` (a mounted volume),
so live-arming survives restarts but stays under your control. On Oracle
Cloud Free Tier (or any Linux VM): install Docker, clone the repo, create
`.env`, `docker compose up -d`.

## Notifications

Alerts fire on: engine start/stop, buys, sells, stop-losses, take-profits,
and errors — to whichever channel(s) you configure (both work; pick one):

- **ntfy (no account, easiest):** install the free *ntfy* app, choose a
  unique topic, set `NTFY_TOPIC` in `.env`, and subscribe to that topic in
  the app. Test with `python -m bot.notify`.
- **Telegram:** set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_OWNER_ID`.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| Engine says "paper mode" though `LIVE_TRADING=true` | You haven't run `python -m bot.golive`, or a health check failed |
| `AuthError` at startup | Wrong key/secret/passphrase or `KUCOIN_KEY_VERSION` in `.env` |
| Signature errors | Server clock skew — sync time (NTP) on the host |
| "insufficient USDT" rejections | Not enough quote balance for the sized order |
| No Telegram alerts | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_OWNER_ID` unset |

## What these guardrails do and don't do

They prevent **technical** disasters — oversized orders, runaway losses,
duplicate fills, a withdrawal-enabled key, trading through an outage. They
**cannot** make a losing strategy profitable. Validate on paper first.
