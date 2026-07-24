"""Central place for every setting the bot reads from the environment.

Secrets (bot token, API keys) never live in code — they come from real
environment variables or a local .env file (see .env.example).
"""

import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# The external API the bot talks to. openFDA works without a key;
# setting one just raises your rate limit (https://open.fda.gov/apis/authentication/).
OPENFDA_BASE_URL = os.getenv("OPENFDA_BASE_URL", "https://api.fda.gov")
OPENFDA_API_KEY = os.getenv("OPENFDA_API_KEY", "")

REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))

# KuCoin — create keys under Account > API Management on kucoin.com.
# For this bot the read-only "General" permission is enough; never give
# a chat bot keys with Trade, Transfer or Withdraw enabled.
KUCOIN_BASE_URL = os.getenv("KUCOIN_BASE_URL", "https://api.kucoin.com")
KUCOIN_API_KEY = os.getenv("KUCOIN_API_KEY", "")
KUCOIN_API_SECRET = os.getenv("KUCOIN_API_SECRET", "")
KUCOIN_API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE", "")
KUCOIN_KEY_VERSION = os.getenv("KUCOIN_KEY_VERSION", "2")

# Your numeric Telegram user id — /balance and /autotrade answer only
# this user. Find yours by messaging @userinfobot on Telegram.
try:
    TELEGRAM_OWNER_ID = int(os.getenv("TELEGRAM_OWNER_ID", "0"))
except ValueError:
    TELEGRAM_OWNER_ID = 0

# --- Auto-trading (signal bot) ---
# LIVE_TRADING is the master safety switch. It stays FALSE until you have
# watched the bot in dry-run and deliberately turn it on:
#   false -> compute & announce trades, place NOTHING (safe default)
#   true  -> send REAL market orders (needs the Trade permission on your key)
LIVE_TRADING = os.getenv("LIVE_TRADING", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
TRADE_SYMBOL = os.getenv("TRADE_SYMBOL", "BTC-USDT")
# Quote-currency amount spent per BUY (e.g. 5 = spend 5 USDT). Keep small.
TRADE_FUNDS_PER_ORDER = os.getenv("TRADE_FUNDS_PER_ORDER", "5")
TRADE_INTERVAL_SECONDS = float(os.getenv("TRADE_INTERVAL_SECONDS", "60"))
KLINE_TYPE = os.getenv("KLINE_TYPE", "5min")  # candle size fed to the strategy
FAST_MA = int(os.getenv("FAST_MA", "9"))
SLOW_MA = int(os.getenv("SLOW_MA", "21"))
# Hard cap: the loop refuses to place more than this many orders per day,
# so a flapping signal or a bug can't drain the account.
MAX_DAILY_ORDERS = int(os.getenv("MAX_DAILY_ORDERS", "10"))
