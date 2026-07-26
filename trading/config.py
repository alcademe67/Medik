"""Settings for the research engine, read from the environment (.env).

Phase 1 needs NO API keys — it uses KuCoin's public market data. Keys are
optional and only raise your rate limit. Everything is overridable via
.env so the later optimization phase can sweep parameters without code
changes.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


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


# --- Exchange / data ---
EXCHANGE_ID = os.getenv("EXCHANGE_ID", "kucoin")
QUOTE_CURRENCY = os.getenv("QUOTE_CURRENCY", "USDT")
TIMEFRAME = os.getenv("TIMEFRAME", "1h")
OHLCV_LIMIT = _i("OHLCV_LIMIT", 500)

# Optional keys (not required for public market data).
KUCOIN_API_KEY = os.getenv("KUCOIN_API_KEY", "")
KUCOIN_API_SECRET = os.getenv("KUCOIN_API_SECRET", "")
KUCOIN_API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE", "")

# --- Scanner ---
MIN_DAILY_VOLUME_USDT = _f("MIN_DAILY_VOLUME_USDT", 1_000_000)
SCANNER_TOP_N = _i("SCANNER_TOP_N", 20)

# --- Backtest ---
FEE_RATE = _f("FEE_RATE", 0.001)  # KuCoin spot taker ~0.1%
INITIAL_CASH = _f("INITIAL_CASH", 10_000)

# Which strategy to run:
#   "trend"   — momentum: buy strength, ride the trend (the original template)
#   "meanrev" — buy the dip, sell high (oversold + below lower Bollinger band)
STRATEGY = os.getenv("STRATEGY", "trend").strip().lower()


@dataclass(frozen=True)
class StrategyParams:
    """Every tunable knob for the strategy + backtest in one place.

    The optimizer (a later phase) varies these; nothing else changes.
    Defaults come from .env so you can also tune without touching code.
    """

    ema_fast: int = _i("EMA_FAST", 21)
    ema_slow: int = _i("EMA_SLOW", 50)
    ema_trend: int = _i("EMA_TREND", 200)
    rsi_period: int = _i("RSI_PERIOD", 14)
    rsi_max: float = _f("RSI_MAX", 70.0)     # don't buy when already overbought
    rsi_min_long: float = _f("RSI_MIN_LONG", 50.0)
    macd_fast: int = _i("MACD_FAST", 12)
    macd_slow: int = _i("MACD_SLOW", 26)
    macd_signal: int = _i("MACD_SIGNAL", 9)
    atr_period: int = _i("ATR_PERIOD", 14)
    atr_stop_mult: float = _f("ATR_STOP_MULT", 2.0)
    risk_reward: float = _f("RISK_REWARD", 2.0)   # take-profit = RR * stop distance
    adx_period: int = _i("ADX_PERIOD", 14)
    adx_min: float = _f("ADX_MIN", 20.0)          # require a real trend
    bb_period: int = _i("BB_PERIOD", 20)
    bb_std: float = _f("BB_STD", 2.0)
    volume_ma: int = _i("VOLUME_MA", 20)          # volume-confirmation window
    # Mean-reversion ("buy the dip") knobs:
    rsi_oversold: float = _f("RSI_OVERSOLD", 30.0)     # buy when RSI dips below this
    rsi_overbought: float = _f("RSI_OVERBOUGHT", 60.0) # sell when RSI rises above this
    meanrev_trend_filter: bool = os.getenv(
        "MEANREV_TREND_FILTER", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}    # only buy dips in an uptrend
    # Exit mode: "atr" (ATR stop + RR target) or "fixed" (fixed-% target/stop,
    # for scalping tests). In fixed mode the two %s below are used instead of ATR.
    exit_mode: str = os.getenv("EXIT_MODE", "atr").strip().lower()
    profit_target_pct: float = _f("PROFIT_TARGET_PCT", 0.5)   # fixed mode: take profit at +this %
    stop_loss_pct: float = _f("STOP_LOSS_PCT", 0.8)           # fixed mode: stop at -this %


DEFAULT_PARAMS = StrategyParams()
