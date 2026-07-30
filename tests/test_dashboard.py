"""Tests for the local browser dashboard (bot/dashboard.py).

Covers the pure state-builder (what the page fetches), the live unrealized-P/L
pricing of open positions, the STOP action, and the thread-safe status holder.
No socket is opened — the HTTP layer is a thin wrapper over these functions.
"""

import pytest

from bot import config, dashboard, state


def _seed(db):
    state.init_db(db)
    state.ensure_day(100.0, db)


def test_livestatus_snapshot_is_an_isolated_copy():
    st = dashboard.LiveStatus()
    st.set_account(balance=42.0, live=True)
    st.set_coin("BTC-USDT", 100.0, "momentum-up", "BUY", 1.5)
    snap = st.snapshot()
    # Mutating the snapshot must not corrupt the internal state.
    snap["coins"]["BTC-USDT"]["price"] = 0.0
    snap["account"]["balance"] = -1
    again = st.snapshot()
    assert again["coins"]["BTC-USDT"]["price"] == 100.0
    assert again["account"]["balance"] == 42.0
    assert again["account"]["live"] is True


def test_build_state_prices_open_positions_live(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FEE_RATE", 0.001)
    db = str(tmp_path / "s.sqlite3")
    _seed(db)
    state.add_position(
        state.Position("BTC-USDT", "buy", 1.0, 100.0, 96.0, 108.0, 0.0, "o", entry_fee=0.1), db
    )
    st = dashboard.LiveStatus()
    st.set_account(balance=50.0, live=True)
    st.set_coin("BTC-USDT", 110.0, "momentum-up", "BUY", 1.0)  # up 10% from entry

    s = dashboard.build_state(st, state_path=db, stop_path=str(tmp_path / "NOPE"))

    assert s["account"]["balance"] == pytest.approx(50.0)
    assert s["account"]["live"] is True
    assert s["account"]["quote"] == config.QUOTE_CURRENCY
    pos = s["positions"][0]
    assert pos["current"] == pytest.approx(110.0)
    assert pos["pct"] == pytest.approx(10.0)
    # gross = (110-100)*1 = 10 ; est exit fee = 110*0.001 = 0.11 ; entry fee 0.1
    assert pos["unrealized_net"] == pytest.approx(10.0 - 0.1 - 0.11)
    # The coin shows up in the live watchlist with its signal.
    assert s["watchlist"][0]["symbol"] == "BTC-USDT"
    assert s["watchlist"][0]["signal"] == "BUY"


def test_build_state_falls_back_to_entry_when_no_live_price(tmp_path):
    db = str(tmp_path / "s.sqlite3")
    _seed(db)
    state.add_position(state.Position("ETH-USDT", "buy", 2.0, 50.0, 48.0, 55.0, 0.0, "e"), db)
    # No set_coin for ETH -> current defaults to entry -> flat, no crash.
    s = dashboard.build_state(dashboard.LiveStatus(), state_path=db, stop_path=str(tmp_path / "NOPE"))
    pos = s["positions"][0]
    assert pos["current"] == pytest.approx(50.0)
    assert pos["pct"] == pytest.approx(0.0)


def test_build_state_includes_trades_and_stats(tmp_path):
    db = str(tmp_path / "s.sqlite3")
    _seed(db)
    pid = state.add_position(state.Position("BTC-USDT", "buy", 1.0, 100.0, 96.0, 108.0, 0.0, "o"), db)
    state.close_position(pid, 108.0, "target", db, entry_fee=0.1, exit_fee=0.1)
    s = dashboard.build_state(dashboard.LiveStatus(), state_path=db, stop_path=str(tmp_path / "NOPE"))
    assert s["stats"]["total_trades"] == 1
    assert s["trades"][0]["symbol"] == "BTC-USDT"
    assert s["trades"][0]["reason"] == "target"
    assert s["trades"][0]["pnl"] == pytest.approx(8.0 - 0.2)   # net after fees


def test_build_state_empty_db_is_safe(tmp_path):
    db = str(tmp_path / "s.sqlite3")
    state.init_db(db)
    s = dashboard.build_state(dashboard.LiveStatus(), state_path=db, stop_path=str(tmp_path / "NOPE"))
    assert s["positions"] == [] and s["trades"] == [] and s["watchlist"] == []
    assert s["account"]["balance"] is None          # engine hasn't reported yet
    assert s["stop_active"] is False


def test_browser_url_maps_localhost_and_wildcard():
    # A server bound to 0.0.0.0 or 127.0.0.1 is opened as localhost in the
    # browser; a real hostname passes through unchanged.
    assert dashboard._browser_url("127.0.0.1", 8787) == "http://localhost:8787"
    assert dashboard._browser_url("0.0.0.0", 8787) == "http://localhost:8787"
    assert dashboard._browser_url("mybox.lan", 9000) == "http://mybox.lan:9000"


def test_maybe_open_browser_respects_the_disable_flag(monkeypatch):
    # With DASHBOARD_OPEN_BROWSER off, no browser thread is spawned at all.
    calls = []
    monkeypatch.setattr(config, "DASHBOARD_OPEN_BROWSER", False)
    monkeypatch.setattr(dashboard.webbrowser, "open", lambda *_a, **_k: calls.append(1))
    dashboard._maybe_open_browser("127.0.0.1", 8787, delay=0)
    import time as _t
    _t.sleep(0.05)
    assert calls == []


def test_request_stop_writes_the_brake_file_and_state_reflects_it(tmp_path):
    stop = str(tmp_path / "STOP")
    db = str(tmp_path / "s.sqlite3")
    state.init_db(db)
    # Before: no stop file -> not active.
    assert dashboard.build_state(dashboard.LiveStatus(), state_path=db, stop_path=stop)["stop_active"] is False
    # The STOP button calls this; it creates the same file the engine watches.
    assert dashboard.request_stop(stop) is True
    import os
    assert os.path.exists(stop)
    # After: build_state now reports the brake as active.
    assert dashboard.build_state(dashboard.LiveStatus(), state_path=db, stop_path=stop)["stop_active"] is True
