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


def test_engine_tick_runs_without_await_typeerror(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.setattr(config, "EMERGENCY_STOP_PATH", str(tmp_path / "STOP"))  # absent
    state.init_db()

    # Flat candles -> HOLD signal, atr 0: exercises the ensure_day line without
    # needing to mock the order path.
    flat = [(100.0, 100.0, 100.0) for _ in range(60)]

    async def fake_candles(_sym, _ktype, limit=200):
        return flat

    async def fake_balance(_cur, account_type="trade"):
        return 1000.0

    monkeypatch.setattr(kucoin_client, "fetch_candles", fake_candles)
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

    # Candles with a high-low range so ATR > 0 (a trade won't size without it).
    candles = [(101.0, 99.0, 100.0) for _ in range(60)]

    async def fake_candles(_sym, _ktype, limit=200):
        return candles

    async def fake_balance(_cur, account_type="trade"):
        return 1000.0

    async def fake_info(sym):
        return dict(symbol=sym, base_min_size=0.0001, quote_min_size=0.1,
                    base_increment=0.0001, price_increment=0.01, enable_trading=True)

    async def fake_notify(*_a, **_k):
        return None

    monkeypatch.setattr(kucoin_client, "fetch_candles", fake_candles)
    monkeypatch.setattr(kucoin_client, "get_available_balance", fake_balance)
    monkeypatch.setattr(kucoin_client, "get_symbol_info", fake_info)
    monkeypatch.setattr(live_engine.notify, "send", fake_notify)
    # Force a BUY on every coin regardless of the MA math, to exercise entries.
    monkeypatch.setattr(live_engine, "crossover_signal", lambda *_a, **_k: live_engine.Signal.BUY)

    engine = live_engine.Engine()
    engine.symbols = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    engine.live = False
    asyncio.run(engine._tick())

    # A distinct paper position was opened for each coin (global caps allow 3).
    open_syms = {p.symbol for p in state.open_positions()}
    assert open_syms == {"BTC-USDT", "ETH-USDT", "SOL-USDT"}


def test_engine_one_bad_coin_does_not_starve_the_others(tmp_path, monkeypatch):
    """A data error on one coin must be isolated — the other coins still trade."""
    monkeypatch.setattr(config, "STATE_DB_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.setattr(config, "EMERGENCY_STOP_PATH", str(tmp_path / "STOP"))
    monkeypatch.setattr(config, "LIVE_TRADING", False)
    state.init_db()

    candles = [(101.0, 99.0, 100.0) for _ in range(60)]

    async def fake_candles(sym, _ktype, limit=200):
        if sym == "BAD-USDT":
            raise kucoin_client.KucoinError("candles endpoint down for this coin")
        return candles

    async def fake_balance(_cur, account_type="trade"):
        return 1000.0

    async def fake_info(sym):
        return dict(symbol=sym, base_min_size=0.0001, quote_min_size=0.1,
                    base_increment=0.0001, price_increment=0.01, enable_trading=True)

    async def fake_notify(*_a, **_k):
        return None

    monkeypatch.setattr(kucoin_client, "fetch_candles", fake_candles)
    monkeypatch.setattr(kucoin_client, "get_available_balance", fake_balance)
    monkeypatch.setattr(kucoin_client, "get_symbol_info", fake_info)
    monkeypatch.setattr(live_engine.notify, "send", fake_notify)
    monkeypatch.setattr(live_engine, "crossover_signal", lambda *_a, **_k: live_engine.Signal.BUY)

    engine = live_engine.Engine()
    engine.symbols = ["BAD-USDT", "ETH-USDT"]
    engine.live = False
    asyncio.run(engine._tick())  # must not raise despite BAD-USDT failing

    open_syms = {p.symbol for p in state.open_positions()}
    assert open_syms == {"ETH-USDT"}   # the healthy coin still traded
