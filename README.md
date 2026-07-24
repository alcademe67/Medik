# Medik

A Telegram bot that shows **how to add an API to a bot** — the same
recipe applied twice:

- **openFDA** (free, no key): `/drug ibuprofen` → the medicine's purpose,
  uses, warnings, and dosage from the FDA drug-label database.
- **KuCoin** (your API keys): `/price BTC` → live market price (public,
  works without keys), and `/balance` → your account balances via
  signed requests (owner-only).

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
├── kucoin_client.py # KuCoin integration (public + signed endpoints)
└── config.py        # tokens & settings, read from environment/.env
.env.example         # template for your local .env (never commit .env)
requirements.txt     # python-telegram-bot, httpx, python-dotenv
```

*Drug information comes from FDA product labels via openFDA and is not
medical advice. Market data comes from KuCoin and is not financial
advice.*
