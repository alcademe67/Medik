"""Unit tests for the trading research engine (Phase 1).

Pure-logic tests only — no network. They cover the indicators, the
scanner ranking, the strategy pipeline, and the backtest metrics.
"""

import numpy as np
import pandas as pd
import pytest

from trading import backtest, indicators, scanner, strategy
from trading.config import StrategyParams


# ---------------------------------------------------------------- indicators
def test_ema_of_constant_is_constant():
    s = pd.Series([5.0] * 50)
    assert indicators.ema(s, 10).round(6).eq(5.0).all()


def test_rsi_bounds_and_extremes():
    up = pd.Series(np.arange(1, 101, dtype=float))       # strictly rising
    down = pd.Series(np.arange(100, 0, -1, dtype=float))  # strictly falling
    rsi_up = indicators.rsi(up, 14).dropna()
    rsi_down = indicators.rsi(down, 14).dropna()
    assert ((rsi_up >= 0) & (rsi_up <= 100)).all()
    assert rsi_up.iloc[-1] > 90   # relentless up -> very high RSI
    assert rsi_down.iloc[-1] < 10  # relentless down -> very low RSI
    flat = pd.Series([42.0] * 30)
    assert indicators.rsi(flat, 14).iloc[-1] == 50.0  # flat -> neutral


def test_atr_non_negative():
    n = 60
    high = pd.Series(np.linspace(10, 20, n)) + 1
    low = pd.Series(np.linspace(10, 20, n)) - 1
    close = pd.Series(np.linspace(10, 20, n))
    a = indicators.atr(high, low, close, 14).dropna()
    assert (a >= 0).all() and a.iloc[-1] > 0


def test_adx_bounded():
    n = 120
    close = pd.Series(np.linspace(10, 40, n))
    high, low = close + 0.5, close - 0.5
    out = indicators.adx(high, low, close, 14)
    adx = out["adx"].dropna()
    assert ((adx >= 0) & (adx <= 100)).all()
    assert adx.iloc[-1] > 20  # a clean straight trend is "trending"


def test_macd_hist_identity():
    close = pd.Series(np.random.default_rng(0).normal(100, 5, 200).cumsum())
    m = indicators.macd(close)
    assert np.allclose((m["macd"] - m["macd_signal"]).values, m["macd_hist"].values, equal_nan=True)


def test_bollinger_ordering():
    close = pd.Series(np.random.default_rng(1).normal(100, 5, 100))
    bb = indicators.bollinger(close, 20, 2.0).dropna()
    assert (bb["bb_upper"] >= bb["bb_mid"]).all()
    assert (bb["bb_mid"] >= bb["bb_lower"]).all()


# ------------------------------------------------------------------- scanner
def test_scanner_rank_filters_and_sorts():
    df = pd.DataFrame(
        {
            "symbol": ["BTC/USDT", "ETH/USDT", "DUST/USDT", "DOGE/USDC"],
            "quote_volume": [5e8, 2e8, 100.0, 9e9],   # DUST illiquid; DOGE wrong quote
            "last": [60000, 3000, 0.0001, 0.1],
            "percentage": [1, 2, 3, 4],
        }
    )
    ranked = scanner.rank(df, quote="USDT", min_volume=1e6, top_n=10)
    assert list(ranked["symbol"]) == ["BTC/USDT", "ETH/USDT"]  # sorted desc, filtered
    assert "DOGE/USDC" not in set(ranked["symbol"])            # wrong quote dropped
    assert "DUST/USDT" not in set(ranked["symbol"])            # below volume floor


def test_scanner_top_n_caps():
    df = pd.DataFrame(
        {"symbol": [f"C{i}/USDT" for i in range(10)],
         "quote_volume": [1e9 - i for i in range(10)],
         "last": [1] * 10, "percentage": [0] * 10}
    )
    assert len(scanner.rank(df, min_volume=0, top_n=3)) == 3


# ------------------------------------------------------------------ strategy
def _synthetic_ohlcv(n=400, seed=7):
    rng = np.random.default_rng(seed)
    # gentle uptrend + noise, with volume
    close = 100 + np.cumsum(rng.normal(0.15, 1.0, n))
    close = np.abs(close) + 1
    high = close + np.abs(rng.normal(0, 0.5, n))
    low = close - np.abs(rng.normal(0, 0.5, n))
    open_ = close + rng.normal(0, 0.3, n)
    volume = np.abs(rng.normal(1000, 300, n))
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def test_strategy_produces_clean_boolean_signals():
    df = _synthetic_ohlcv()
    out = strategy.generate_signals(df, StrategyParams())
    for col in ("entry", "exit", "rsi", "atr", "adx", "macd_hist"):
        assert col in out.columns
    assert out["entry"].dtype == bool and out["exit"].dtype == bool
    assert not out["entry"].isna().any() and not out["exit"].isna().any()
    # entries must never fire below the trend EMA (uptrend filter)
    entered = out[out["entry"]]
    assert (entered["close"] > entered["ema_trend"]).all()


# ------------------------------------------------------------------ backtest
def test_bars_per_year():
    assert backtest.bars_per_year("1h") == pytest.approx(8760)
    assert backtest.bars_per_year("1d") == pytest.approx(365)


def test_metrics_from_known_curve():
    res = backtest.BacktestResult(
        trades=[
            backtest.Trade(0, 1, 100, 110, "target", 10.0),
            backtest.Trade(1, 2, 110, 104.5, "stop", -5.0),
            backtest.Trade(2, 3, 104.5, 120, "target", 15.0),
            backtest.Trade(3, 4, 120, 114, "stop", -5.0),
        ],
        equity=pd.Series([100.0, 110.0, 99.0, 120.0]),
        initial_cash=100.0,
        timeframe="1d",
    )
    backtest._metrics(res)
    assert res.total_return_pct == pytest.approx(20.0)         # 100 -> 120
    assert res.max_drawdown_pct == pytest.approx(-10.0)        # 110 -> 99
    assert res.win_rate_pct == pytest.approx(50.0)             # 2 of 4
    assert res.profit_factor == pytest.approx(25 / 10)         # (10+15)/(5+5)


def test_backtest_runs_end_to_end():
    df = _synthetic_ohlcv()
    res = backtest.run(df, StrategyParams(), timeframe="1h", initial_cash=10_000, fee_rate=0.001)
    assert res.final_equity > 0
    assert len(res.equity) == len(df)
    assert res.num_trades >= 0
    assert -100 <= res.max_drawdown_pct <= 0
    # every trade's stated return matches its entry/exit prices
    for t in res.trades:
        assert t.return_pct == pytest.approx((t.exit_price - t.entry_price) / t.entry_price * 100)


def test_backtest_no_orders_module_imported():
    # Guard: the research engine must not import any execution/order module.
    import trading
    assert not hasattr(trading, "execution")
