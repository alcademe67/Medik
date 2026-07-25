"""Central place for every setting the bot reads from the environment.

Secrets (bot token, API keys) never live in code — they come from real
environment variables or a local .env file (see .env.example).
"""

import os
from dataclasses import dataclass

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

# --- Notifications: ntfy (free phone push, no account needed) ---
# Pick a UNIQUE, hard-to-guess topic name (ntfy.sh topics are public, so
# treat the name like a password), put it here, and subscribe to the same
# topic in the ntfy phone app. That's the whole setup.
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")

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


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# --- LIVE trading safety gates ---
# Live orders require ALL of these, checked at startup (see bot/preflight.py):
#   1. LIVE_TRADING=true
#   2. valid API keys (auth succeeds)
#   3. the operator ran `python -m bot.golive` and confirmed the checklist
# The go-live confirmation writes a token file; the live engine refuses to
# start without it even if LIVE_TRADING=true. Two independent switches.
GOLIVE_TOKEN_PATH = os.getenv("GOLIVE_TOKEN_PATH", ".golive_confirmed")
STATE_DB_PATH = os.getenv("STATE_DB_PATH", "bot_state.sqlite3")

# Emergency brake: create this file (default "STOP") to halt trading and
# shut the engine down gracefully — no terminal access needed.
EMERGENCY_STOP_PATH = os.getenv("EMERGENCY_STOP_PATH", "STOP")

# Quote currency of the account we size risk against (USDT for BTC-USDT).
QUOTE_CURRENCY = os.getenv("QUOTE_CURRENCY", "USDT")

# --- Logging ---
# Rotating daily log files land here; the newest is bot.log, older ones are
# bot.log.YYYY-MM-DD. Kept for LOG_BACKUP_DAYS days.
LOG_DIR = os.getenv("LOG_DIR", "logs")
LOG_BACKUP_DAYS = _i("LOG_BACKUP_DAYS", 30)


@dataclass(frozen=True)
class RiskParams:
    """Every risk control in one place. All overridable via .env."""

    max_risk_per_trade: float = _f("MAX_RISK_PER_TRADE", 0.01)   # 1% of equity
    max_open_positions: int = _i("MAX_OPEN_POSITIONS", 3)
    daily_loss_limit_pct: float = _f("DAILY_LOSS_LIMIT_PCT", 5.0)  # halt for the day
    max_consecutive_losses: int = _i("MAX_CONSECUTIVE_LOSSES", 3)  # then pause
    stop_atr_mult: float = _f("STOP_ATR_MULT", 2.0)               # stop = entry - k*ATR
    take_profit_rr: float = _f("TAKE_PROFIT_RR", 2.0)             # target = RR * stop dist
    # Absolute floor on order notional; the real minimum from the exchange
    # is also enforced per-symbol in execution.py.
    min_order_usdt: float = _f("MIN_ORDER_USDT", 1.0)
    # Minimum seconds between order submissions (rate-limit courtesy).
    order_min_interval_s: float = _f("ORDER_MIN_INTERVAL_S", 1.0)
    # Stop management (0 disables each). Stops only ever move up.
    breakeven_trigger_pct: float = _f("BREAKEVEN_TRIGGER_PCT", 0.0)  # +X% -> stop to entry
    trailing_atr_mult: float = _f("TRAILING_ATR_MULT", 0.0)         # trail this*ATR below high


RISK = RiskParams()
