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
| Risk per trade | 1% of equity | `MAX_RISK_PER_TRADE` |
| Max open positions | 3 | `MAX_OPEN_POSITIONS` |
| Daily loss limit | 5% | `DAILY_LOSS_LIMIT_PCT` |
| Pause after N losses | 3 | `MAX_CONSECUTIVE_LOSSES` |
| Stop-loss | 2×ATR | `STOP_ATR_MULT` |
| Take-profit | 2:1 | `TAKE_PROFIT_RR` |

## Recommended path to live (do not skip)

1. **Backtest** a strategy until it shows an edge: `python -m trading backtest-top 1h`.
2. **Paper trade** it: run the engine with `LIVE_TRADING=false` for weeks and
   watch the Telegram alerts and dashboard.
3. **Create a KuCoin key** with **Trade** permission and **no** withdrawal
   permission. Put it in `.env`.
4. **Arm it**: `python -m bot.golive` — pass the health checks, answer the
   attestations honestly, type the confirmation phrase.
5. Set `LIVE_TRADING=true`, start small (`MAX_RISK_PER_TRADE=0.005` or less).

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

Telegram alerts fire on: engine start/stop, buys, sells, stop-losses,
take-profits, and errors. Configure `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_OWNER_ID` in `.env` (see the main README).

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
