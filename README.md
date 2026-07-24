# Medik

A Telegram bot that shows **how to add an API to a bot**: it takes a chat
command, calls an external HTTP API (the free [openFDA](https://open.fda.gov/apis/drug/label/)
drug-label API), and turns the JSON response into a readable reply.

Send it `/drug ibuprofen` and it answers with the medicine's purpose,
uses, warnings, and dosage straight from the FDA database.

## The recipe: adding any API to any bot

The same five steps apply whether your bot runs on Telegram, Discord, or
WhatsApp, and whatever API you're calling:

1. **Keep credentials out of the code.** Bot tokens and API keys go in
   environment variables (here: a `.env` file loaded by
   [`bot/config.py`](bot/config.py), with `.env` listed in `.gitignore`).
2. **Put all API calls in one module.** [`bot/api_client.py`](bot/api_client.py)
   is the only file that knows the API's URL, parameters, and JSON shape.
   It exposes one function (`fetch_drug_label`) that returns a plain dict.
   Swapping APIs later means rewriting one file, not the whole bot.
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
/drug tylenol
```

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
├── main.py        # entry point: commands, formatting, error replies
├── api_client.py  # the API integration — all HTTP lives here
└── config.py      # tokens & settings, read from environment/.env
.env.example       # template for your local .env (never commit .env)
requirements.txt   # python-telegram-bot, httpx, python-dotenv
```

*Drug information comes from FDA product labels via openFDA and is not
medical advice.*
