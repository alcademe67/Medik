"""Regression test for the `await state.ensure_day(...)` startup crash.

`ensure_day` is synchronous; awaiting its None return raised
"TypeError: object NoneType can't be used in 'await' expression". This
runs a full engine tick (paper mode, network mocked) and asserts it
completes without that exception.
"""

import asyncio
import inspect

from bot import config, kucoin_client, live_engine, state


def test_ensure_day_is_synchronous():
    # Guard: if someone makes ensure_day async (or re-adds the await), this fails.
    assert not inspect.iscoroutinefunction(state.ensure_day)


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
