"""Regression test for the `await state.ensure_day(...)` startup crash.

`ensure_day` is synchronous; awaiting its None return raised
"TypeError: object NoneType can't be used in 'await' expression". This
runs a full engine tick (paper mode, network mocked) and asserts it
completes without that exception.
"""

import asyncio
import inspect

from bot import config, kucoin_client, live_engine, state
from bot.config import _parse_symbols


def _ohlcv(n, close=100.0, spread=1.0):
    """n OHLCV dict rows (oldest to newest) with a high-low range so ATR > 0."""
    return [
        {"time": 1_600_000_000 + i * 3600, "open": close, "high": close + spread,
         "low": close - spread, "close": close, "volume": 1000.0}
        for i in range(n)
    ]


def test_ensure_day_is_synchronous():
    # Guard: if someone makes ensure_day async (or re-adds the await), this fails.
    assert not inspect.iscoroutinefunction(state.ensure_day)


def test_parse_symbols_dedupes_uppercases_and_falls_back():
    # Commas and/or spaces both separate; order is preserved.
    assert _parse_symbols("BTC-USDT, ETH-USDT SOL-USDT", "X-USDT") == [
        "BTC-USDT", "ETH-USDT", "SOL-USDT",
    ]
    # Case-folded to upper and de-duplicated.
    assert _parse_symbols("btc-usdt, BTC-USDT", "X-USDT") == ["BTC-USDT"]
    # Empty -> the single fallback symbol (also uppercased).
    assert _parse_symbols("   ", "doge-usdt") == ["DOGE-USDT"]
    # Empty with a LIST fallback -> the whole list (uppercased); explicit wins.
    assert _parse_symbols("", ["btc-usdt", "eth-usdt"]) == ["BTC-USDT", "ETH-USDT"]
    assert _parse_symbols("SOL-USDT", ["btc-usdt"]) == ["SOL-USDT"]


def test_engine_tick_runs_without_await_typeerror(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.setattr(config, "EMERGENCY_STOP_PATH", str(tmp_path / "STOP"))  # absent
    state.init_db()

    # Only 60 bars -> the regime signal is "warming-up" (needs EMA_TREND+2),
    # so HOLD: exercises the ensure_day line without mocking the order path.
    async def fake_ohlcv(_sym, _ktype, limit=400):
        return _ohlcv(60)

    async def fake_balance(_cur, account_type="trade"):
        return 1000.0

    monkeypatch.setattr(kucoin_client, "fetch_ohlcv", fake_ohlcv)
    monkeypatch.setattr(kucoin_client, "get_available_balance", fake_balance)

    engine = live_engine.Engine()
    engine.live = False
    # Used to raise: TypeError: object NoneType can't be used in 'await' expression
    asyncio.run(engine._tick())

    # Proof ensure_day actually executed and recorded the day.
    assert state.today_start_equity() == 1000.0


def test_engine_opens_positions_across_the_watchlist(tmp_path, monkeypatch):
    """One tick should open a position on EACH coin in the watchlist — proof the
    engine is no longer stuck on a single symbol."""
    monkeypatch.setattr(config, "STATE_DB_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.setattr(config, "EMERGENCY_STOP_PATH", str(tmp_path / "STOP"))  # absent
    monkeypatch.setattr(config, "LIVE_TRADING", False)                          # paper mode
    state.init_db()

    async def fake_ohlcv(_sym, _ktype, limit=400):
        return _ohlcv(60)      # range gives ATR > 0; the regime signal is forced below

    async def fake_balance(_cur, account_type="trade"):
        return 1000.0

    async def fake_info(sym):
        return dict(symbol=sym, base_min_size=0.0001, quote_min_size=0.1,
                    base_increment=0.0001, price_increment=0.01, enable_trading=True)

    async def fake_notify(*_a, **_k):
        return None

    monkeypatch.setattr(kucoin_client, "fetch_ohlcv", fake_ohlcv)
    monkeypatch.setattr(kucoin_client, "get_available_balance", fake_balance)
    monkeypatch.setattr(kucoin_client, "get_symbol_info", fake_info)
    monkeypatch.setattr(live_engine.notify, "send", fake_notify)
    # Force a regime BUY on every coin, to exercise the entry path.
    monkeypatch.setattr(
        live_engine.regime_signal, "signal_from_frame",
        lambda *_a, **_k: (live_engine.Signal.BUY, "bull"),
    )

    engine = live_engine.Engine()
    engine.symbols = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    engine.live = False
    asyncio.run(engine._tick())

    # A distinct paper position was opened for each coin (global caps allow 3).
    open_syms = {p.symbol for p in state.open_positions()}
    assert open_syms == {"BTC-USDT", "ETH-USDT", "SOL-USDT"}


def test_reconcile_removes_phantom_keeps_real(tmp_path, monkeypatch):
    """Startup reconciliation drops a recovered position the exchange doesn't
    hold (phantom) and keeps one it does."""
    monkeypatch.setattr(config, "STATE_DB_PATH", str(tmp_path / "s.sqlite3"))
    state.init_db()
    state.add_position(state.Position("BTC-USDT", "buy", 0.01, 100, 96, 108, 0.0, "b"))
    state.add_position(state.Position("ETH-USDT", "buy", 0.50, 100, 96, 108, 0.0, "e"))

    async def bal(cur, account_type="trade"):
        return {"BTC": 0.0, "ETH": 0.5}.get(cur, 0.0)   # no BTC on exchange, ETH present

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(kucoin_client, "get_available_balance", bal)
    monkeypatch.setattr(live_engine.notify, "send", noop)

    eng = live_engine.Engine()
    eng.live = True
    asyncio.run(eng._reconcile_positions())

    assert {p.symbol for p in state.open_positions()} == {"ETH-USDT"}  # phantom BTC gone


def test_no_order_submitted_while_warming_up(tmp_path, monkeypatch):
    """regime=warming-up must submit NOTHING — even an in-the-money exit is skipped."""
    monkeypatch.setattr(config, "STATE_DB_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.setattr(config, "EMERGENCY_STOP_PATH", str(tmp_path / "STOP"))
    monkeypatch.setattr(config, "LIVE_TRADING", True)
    state.init_db()
    # Target 99 < the flat price 100, so management WOULD sell if it ran.
    state.add_position(state.Position("BTC-USDT", "buy", 0.01, 100, 96, 99, 0.0, "o"))

    async def fake_ohlcv(_s, _k, limit=400):
        return _ohlcv(30)     # 30 bars < min_bars -> regime is "warming-up"

    async def fake_balance(_c, account_type="trade"):
        return 1000.0

    orders = {"n": 0}

    async def place(*_a, **_k):
        orders["n"] += 1
        return {"dryRun": False, "orderId": "x", "order": {}}

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(kucoin_client, "fetch_ohlcv", fake_ohlcv)
    monkeypatch.setattr(kucoin_client, "get_available_balance", fake_balance)
    monkeypatch.setattr(kucoin_client, "place_market_order", place)
    monkeypatch.setattr(live_engine.notify, "send", noop)

    eng = live_engine.Engine()
    eng.symbols = ["BTC-USDT"]
    eng.live = True
    asyncio.run(eng._tick_symbol("BTC-USDT"))

    assert orders["n"] == 0            # nothing submitted while warming-up
    assert state.count_open() == 1     # the position is left untouched


def test_engine_one_bad_coin_does_not_starve_the_others(tmp_path, monkeypatch):
    """A data error on one coin must be isolated — the other coins still trade."""
    monkeypatch.setattr(config, "STATE_DB_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.setattr(config, "EMERGENCY_STOP_PATH", str(tmp_path / "STOP"))
    monkeypatch.setattr(config, "LIVE_TRADING", False)
    state.init_db()

    async def fake_ohlcv(sym, _ktype, limit=400):
        if sym == "BAD-USDT":
            raise kucoin_client.KucoinError("candles endpoint down for this coin")
        return _ohlcv(60)

    async def fake_balance(_cur, account_type="trade"):
        return 1000.0

    async def fake_info(sym):
        return dict(symbol=sym, base_min_size=0.0001, quote_min_size=0.1,
                    base_increment=0.0001, price_increment=0.01, enable_trading=True)

    async def fake_notify(*_a, **_k):
        return None

    monkeypatch.setattr(kucoin_client, "fetch_ohlcv", fake_ohlcv)
    monkeypatch.setattr(kucoin_client, "get_available_balance", fake_balance)
    monkeypatch.setattr(kucoin_client, "get_symbol_info", fake_info)
    monkeypatch.setattr(live_engine.notify, "send", fake_notify)
    monkeypatch.setattr(
        live_engine.regime_signal, "signal_from_frame",
        lambda *_a, **_k: (live_engine.Signal.BUY, "bull"),
    )

    engine = live_engine.Engine()
    engine.symbols = ["BAD-USDT", "ETH-USDT"]
    engine.live = False
    asyncio.run(engine._tick())  # must not raise despite BAD-USDT failing

    open_syms = {p.symbol for p in state.open_positions()}
    assert open_syms == {"ETH-USDT"}   # the healthy coin still traded
