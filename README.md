# Medik

A Telegram bot that shows **how to add an API to a bot** — the same
recipe applied twice:

- **openFDA** (free, no key): `/drug ibuprofen` → the medicine's purpose,
  uses, warnings, and dosage from the FDA drug-label database.
- **KuCoin** (your API keys): `/price BTC` → live market price (public,
  works without keys), `/balance` → your account balances via signed
  requests (owner-only), and `/autotrade` → a signal-based trading bot
  that **defaults to dry-run and places no real orders** until you turn
  live trading on yourself.

## The recipe: adding any API to any bot

The same five steps apply whether your bot runs on Telegram, Discord, or
WhatsApp, and whatever API you're calling:

1. **Keep credentials out of the code.** Bot tokens and API keys go in
   environment variables (here: a `.env` file loaded by
   [`bot/config.py`](bot/config.py), with `.env` listed in `.gitignore`).
2. **Put all API calls in one module per API.** [`bot/api_client.py`](bot/api_client.py)
   is the only file that knows openFDA's URL, parameters, and JSON shape;
   [`bot/kucoin_client.py`](bot/kucoin_client.py) is the only file that
   knows KuCoin's. Each exposes plain functions returning plain dicts.
   Swapping or adding APIs means touching one file, not the whole bot.
3. **Call that module from your command handler.** In
   [`bot/main.py`](bot/main.py), the `/drug` handler parses the user's
   text, awaits `api_client.fetch_drug_label(...)`, and formats the result.
   Use an **async** HTTP client (`httpx.AsyncClient`) so one slow API call
   doesn't freeze the bot for every other user.
4. **Handle the three ways an API call fails** — and answer the user in
   plain language for each:
   - *No data for the query* → "No FDA label found for X, try the generic name."
   - *API down / timeout* → "The database is not responding, try again later."
   - *Unexpected response shape* → treated like an outage, logged for you.
5. **Format for chat.** Truncate long fields (Telegram caps messages at
   4096 chars), escape user/API text before using HTML mode, and add any
   required disclaimers.

## Run it

```bash
git clone https://github.com/alcademe67/Medik.git
cd Medik
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then paste in your token from @BotFather
python -m bot.main
```

Open your bot in Telegram and try:

```
/drug ibuprofen
/price BTC
/price ETH-EUR
```

`/drug` and `/price` work with no API keys at all — only `/balance`
needs your KuCoin credentials.

## Hooking up your KuCoin keys

1. On kucoin.com go to **Account → API Management → Create API**. You'll
   choose an **API passphrase** (remember it — the bot needs it too) and
   get an **API key** and **API secret**. Tick **only the "General"
   permission**: this bot just reads data, and a chat bot should never
   hold keys that can trade or withdraw funds.
2. Open your `.env` and paste in all three values, plus your own
   Telegram id so nobody else can ask the bot for your balances:

   ```
   KUCOIN_API_KEY=6423abc...
   KUCOIN_API_SECRET=f81c2d90-...
   KUCOIN_API_PASSPHRASE=the-passphrase-you-chose
   KUCOIN_KEY_VERSION=2
   TELEGRAM_OWNER_ID=123456789     # from @userinfobot on Telegram
   ```
3. Restart the bot and send it `/balance`.

How the authentication works: for every private request the client signs
`timestamp + METHOD + path + body` with your API secret (HMAC-SHA256,
base64) and sends the result in the `KC-API-*` headers — see
`_signed_headers()` in [`bot/kucoin_client.py`](bot/kucoin_client.py).
That's the pattern most exchange APIs use, so it ports directly.

If a key ever leaks (pasted in a chat, committed by accident), delete it
in KuCoin's API Management immediately and create a fresh one.

## Auto-trading (signal bot) — read before you go live

> **Money warning.** Automated trading can lose money quickly, and simple
> strategies like the built-in one usually do. Nothing here is financial
> advice. Treat live trading as spending money you can afford to lose.

The bot ships in **dry-run mode**: it computes signals and tells you the
trades it *would* make, but places **no real orders**. This lets you see
the whole thing work before a cent is at risk. The safe path:

1. **Watch it on paper.** With `LIVE_TRADING=false` (the default), send
   `/autotrade on`. The bot messages you every time its strategy would
   buy or sell. Leave it running for a while and see if you like what it
   does. Stop with `/autotrade off`, check state with `/autotrade status`.
2. **Understand the strategy.** It's a moving-average crossover in
   [`bot/strategy.py`](bot/strategy.py) — buy when the fast average
   crosses above the slow one, sell when it crosses back. Tune `FAST_MA`,
   `SLOW_MA`, `KLINE_TYPE`, and `TRADE_SYMBOL` in `.env`, or rewrite that
   one file with your own rule.
   **Test it on past prices first** with the backtester (below) — it
   needs no keys and no risk.
3. **Only then, go live — deliberately.** To place real orders you must:
   - create a KuCoin key with the **Trade** permission enabled (the
     read-only key from above can't trade), and
   - set `LIVE_TRADING=true` in `.env`, then restart the bot.

   Even live, it's bounded by `TRADE_FUNDS_PER_ORDER` (keep it tiny, e.g.
   5 USDT) and `MAX_DAILY_ORDERS` (a hard daily cap so a bug or a flapping
   signal can't keep firing). Start with the smallest amounts KuCoin
   allows.

The order-placement code (`place_market_order` in
[`bot/kucoin_client.py`](bot/kucoin_client.py)) physically refuses to hit
the network unless `LIVE_TRADING` is true, so dry-run cannot spend money
even if something else has a bug.

### Backtesting — try the strategy on history first

Before dry-run, before live, see how the strategy *would have* done on
real past prices. No API keys needed (candle data is public):

```bash
python -m bot.backtest                 # uses TRADE_SYMBOL / KLINE_TYPE from .env
python -m bot.backtest ETH-USDT 15min  # or pick a coin and candle size
```

You get a report like:

```
Backtest — BTC-USDT @ 5min  (1500 candles)
Strategy: MA crossover (9/21), fee 0.10% per trade
--------------------------------------------
Round-trip trades : 23
Win rate          : 43.5%
Strategy return   : +2.14%
Buy & hold return : +5.80%
Max drawdown      : 8.20%
Final equity      : 1021.40 (from 1000)
--------------------------------------------
Verdict: strategy LAGGED buy & hold by -3.66 points.
Past performance does not predict future results.
```

Or run it in Telegram: `/backtest BTC 1hour`. The backtester replays the
**same** signal function the live bot uses ([`bot/strategy.py`](bot/strategy.py)),
so what you measure is what you'd trade. Two honest caveats: it assumes
you fill exactly at each candle's close (real fills are slightly worse),
and a good past result is never a promise about the future. The number
that matters most is the **edge over buy & hold** — if the strategy can't
beat simply holding the coin, it isn't worth running.

## Swap in your own API

1. Change `OPENFDA_BASE_URL` (and key) in `.env` / `bot/config.py` to your
   API's address, e.g. a weather or news API.
2. Rewrite `fetch_drug_label` in `bot/api_client.py` into a function for
   your endpoint: set the path and query parameters, keep the
   404 → `NotFoundError` and everything-else → `ApiError` pattern.
3. Point a new `CommandHandler` in `bot/main.py` at a handler that calls
   your function and formats its dict into a message.

If the API needs a key sent as a header instead of a query parameter, add
it to the `headers=` dict where the `httpx.AsyncClient` is created.

## Project layout

```
bot/
├── main.py          # entry point: commands, formatting, error replies
├── api_client.py    # openFDA integration (keyless API example)
├── kucoin_client.py # KuCoin integration (public + signed + order placement)
├── strategy.py      # trading signal logic (pure, swap in your own rule)
├── trader.py        # the autonomous loop (dry-run by default)
├── backtest.py      # replay the strategy on historical prices
└── config.py        # tokens & settings, read from environment/.env
.env.example         # template for your local .env (never commit .env)
requirements.txt     # python-telegram-bot, httpx, python-dotenv
```

*Drug information comes from FDA product labels via openFDA and is not
medical advice. Market data comes from KuCoin and is not financial
advice.*
