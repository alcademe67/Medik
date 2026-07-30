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

# Default watchlist when TRADE_SYMBOLS is not set in .env — liquid majors, so
# out of the box the bot has several coins to find a setup on (rather than one
# quiet market). All KuCoin USDT pairs, deep liquidity, small minimum orders.
DEFAULT_SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT",
    "DOGE-USDT", "AVAX-USDT", "DOT-USDT", "LTC-USDT", "ATOM-USDT",
]


def _parse_symbols(raw: str, fallback) -> list[str]:
    """Parse a TRADE_SYMBOLS value into an ordered, de-duplicated list.

    Accepts commas and/or whitespace as separators ("BTC-USDT, ETH-USDT").
    Falls back to `fallback` (a symbol string, or a list of symbols) when
    nothing is configured.
    """
    syms = list(dict.fromkeys(s.upper() for s in raw.replace(",", " ").split()))
    if syms:
        return syms
    if isinstance(fallback, (list, tuple)):
        return [s.upper() for s in fallback]
    return [fallback.upper()]


# Watchlist: the live engine trades EACH of these coins, not just one, giving
# more opportunities than a single symbol. Comma/space separated in .env, e.g.
#   TRADE_SYMBOLS=BTC-USDT, ETH-USDT, SOL-USDT
# Unset -> DEFAULT_SYMBOLS (a few liquid majors). Every coin shares the SAME
# global risk caps (max open positions, daily loss limit, daily order cap), so
# widening the list adds breadth without loosening any safety limit.
TRADE_SYMBOLS = _parse_symbols(os.getenv("TRADE_SYMBOLS", ""), DEFAULT_SYMBOLS)
# Quote-currency amount spent per BUY (e.g. 5 = spend 5 USDT). Keep small.
TRADE_FUNDS_PER_ORDER = os.getenv("TRADE_FUNDS_PER_ORDER", "5")
TRADE_INTERVAL_SECONDS = float(os.getenv("TRADE_INTERVAL_SECONDS", "60"))
# Signal mode for the live engine:
#   "regime"   — careful: trades only genuine setups (can hold for hours)
#   "momentum" — aggressive: buys any coin in a short-term uptrend (fast EMA >
#                slow EMA), rides until the trend breaks or the stop/target hits.
#                Trades far more often — and with no proven edge, tends to lose
#                faster on fees + whipsaw. Chosen deliberately.
TRADE_MODE = os.getenv("TRADE_MODE", "momentum").strip().lower()
# Candle size fed to the strategy. 1min = fast/reactive (jumps on bounces sooner,
# more trades + more fees); 5min/15min = slower/steadier.
KLINE_TYPE = os.getenv("KLINE_TYPE", "1min").strip()
FAST_MA = int(os.getenv("FAST_MA", "9"))
SLOW_MA = int(os.getenv("SLOW_MA", "21"))
# Candles pulled per tick for the regime signal. Must comfortably exceed the
# trend EMA length (EMA_TREND, default 200) so the regime is warmed up before
# the bot acts. Raise this if you raise EMA_TREND.
REGIME_CANDLES = int(os.getenv("REGIME_CANDLES", "400"))
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

# Taker fee rate PER SIDE, used only to ESTIMATE fees in paper mode (live
# trading uses the real fee reported by KuCoin fills). KuCoin spot taker ~0.1%.
FEE_RATE = _f("FEE_RATE", 0.001)

# --- Logging ---
# Rotating daily log files land here; the newest is bot.log, older ones are
# bot.log.YYYY-MM-DD. Kept for LOG_BACKUP_DAYS days.
LOG_DIR = os.getenv("LOG_DIR", "logs")
LOG_BACKUP_DAYS = _i("LOG_BACKUP_DAYS", 30)

# --- Local web dashboard (bot/dashboard.py) ---
# A browser view of the bot (balance, positions, live signals, STOP button)
# instead of reading the terminal. The engine starts it automatically; open the
# URL below. Bound to 127.0.0.1 (localhost) ONLY by default: it shows balances
# and can halt the bot, so it must never be reachable from the network. Set
# DASHBOARD_HOST=0.0.0.0 only inside an isolated container with mapped ports.
DASHBOARD_ENABLED = os.getenv("DASHBOARD_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on",
}
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = _i("DASHBOARD_PORT", 8787)
# Auto-open the dashboard in the default browser when the engine starts, so the
# operator never has to type a web address. On by default (for the desktop
# launcher); set DASHBOARD_OPEN_BROWSER=false for headless / container runs.
DASHBOARD_OPEN_BROWSER = os.getenv("DASHBOARD_OPEN_BROWSER", "true").strip().lower() in {
    "1", "true", "yes", "on",
}


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
    # Hard ceiling on how much equity a SINGLE position may deploy (its
    # notional), independent of the risk-to-stop sizing above. With tight
    # intraday stops the risk sizing can otherwise want ~100% of equity in
    # one trade; this keeps any one position to a small slice of the account.
    # 0 disables the cap. Expressed as a percent of equity.
    max_position_pct: float = _f("MAX_POSITION_PCT", 25.0)
    # Fraction of the available quote balance to HOLD BACK when sizing to cash,
    # so a market order's taker fee + slippage can't push the cost above the
    # balance (KuCoin error 200004 "Balance insufficient"). Sizing never targets
    # 100% of funds. Raise it to keep a bigger reserve (e.g. 0.10 => spend ≤90%).
    cash_buffer: float = _f("ORDER_CASH_BUFFER", 0.02)
    # Hard cap on entry (BUY) orders per UTC day, enforced by the live engine
    # against the persisted trade history (survives restarts). A flapping
    # signal or a bug cannot churn more than this many entries in a day.
    max_daily_orders: int = _i("MAX_DAILY_ORDERS", 10)
    # Minimum seconds between order submissions (rate-limit courtesy).
    order_min_interval_s: float = _f("ORDER_MIN_INTERVAL_S", 1.0)
    # Stop management (0 disables each). Stops only ever move up.
    breakeven_trigger_pct: float = _f("BREAKEVEN_TRIGGER_PCT", 0.0)  # +X% -> stop to entry
    trailing_atr_mult: float = _f("TRAILING_ATR_MULT", 0.0)         # trail this*ATR below high


RISK = RiskParams()
