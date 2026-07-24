"""Safety tests for the live-trading path.

Covers the risk manager, the pre-trade validation, the persistence layer,
and the live gate. All offline — network calls to KuCoin are monkeypatched.
These guard the properties that keep real money safe.
"""

import asyncio

import pytest

from bot import config, execution, kucoin_client, preflight, risk, state
from bot.config import RiskParams


# ------------------------------------------------------------ risk manager
def test_position_size_risks_configured_fraction():
    # equity 10000, 1% risk = 100. entry 100, stop 90 => 10/unit => 10 units.
    size = risk.position_size(equity=10_000, entry_price=100, stop_price=90, available_quote=1e9)
    assert size == pytest.approx(10.0)


def test_position_size_capped_by_available_cash():
    size = risk.position_size(equity=10_000, entry_price=100, stop_price=90, available_quote=500)
    assert size == pytest.approx(5.0)  # can only afford 5 units at 100


def test_position_size_zero_on_bad_inputs():
    assert risk.position_size(equity=0, entry_price=100, stop_price=90, available_quote=100) == 0
    assert risk.position_size(equity=100, entry_price=100, stop_price=100, available_quote=100) == 0


def test_stop_and_target_math():
    stop, target = risk.stop_and_target(100, atr=5, params=RiskParams(stop_atr_mult=2, take_profit_rr=2))
    assert stop == pytest.approx(90.0)          # 100 - 2*5
    assert target == pytest.approx(120.0)       # 100 + 2*(100-90)


def test_check_can_trade_enforces_every_limit():
    p = RiskParams(max_open_positions=3, max_consecutive_losses=3, daily_loss_limit_pct=5)
    assert not risk.check_can_trade(open_positions=3, realized_pnl_today=0,
                                    start_equity_today=1000, consecutive_losses=0, params=p).allowed
    assert not risk.check_can_trade(open_positions=0, realized_pnl_today=0,
                                    start_equity_today=1000, consecutive_losses=3, params=p).allowed
    assert not risk.check_can_trade(open_positions=0, realized_pnl_today=-60,
                                    start_equity_today=1000, consecutive_losses=0, params=p).allowed  # -6%
    assert risk.check_can_trade(open_positions=1, realized_pnl_today=-10,
                                start_equity_today=1000, consecutive_losses=1, params=p).allowed


# --------------------------------------------------------------- persistence
def test_state_roundtrip_and_stats(tmp_path):
    db = str(tmp_path / "s.sqlite3")
    state.init_db(db)
    state.ensure_day(1000, db)
    pid = state.add_position(
        state.Position("BTC-USDT", "buy", 1.0, 100, 90, 120, 0.0, "oid1"), db
    )
    assert state.count_open(db) == 1 and state.has_open_symbol("BTC-USDT", db)
    pnl = state.close_position(pid, 110, "target", db)
    assert pnl == pytest.approx(10.0)
    assert state.count_open(db) == 0
    assert state.today_realized_pnl(db) == pytest.approx(10.0)
    st = state.stats(db)
    assert st["total_trades"] == 1 and st["win_rate"] == 100.0


def test_state_tracks_losing_streak(tmp_path):
    db = str(tmp_path / "s.sqlite3")
    state.init_db(db)
    state.ensure_day(1000, db)
    pid = state.add_position(state.Position("BTC-USDT", "buy", 1.0, 100, 90, 120, 0.0, "a"), db)
    state.close_position(pid, 95, "stop", db)   # -5 loss
    assert state.consecutive_losses(db) == 1
    assert state.today_realized_pnl(db) == pytest.approx(-5.0)


# ---------------------------------------------------- pre-trade validation
def _symbol_info(**over):
    base = dict(symbol="BTC-USDT", base_min_size=0.001, quote_min_size=0.1,
                base_increment=0.0001, price_increment=0.01, enable_trading=True)
    base.update(over)
    return base


def test_validate_rejects_below_min_size(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DB_PATH", str(tmp_path / "s.sqlite3"))
    state.init_db()
    async def info(_s): return _symbol_info(base_min_size=1.0)
    monkeypatch.setattr(kucoin_client, "get_symbol_info", info)
    with pytest.raises(execution.ValidationError):
        asyncio.run(execution.validate_buy("BTC-USDT", 0.5, 100.0, live=False))


def test_validate_insufficient_funds_live_only(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DB_PATH", str(tmp_path / "s.sqlite3"))
    state.init_db()
    async def info(_s): return _symbol_info()
    async def bal(_c, account_type="trade"): return 10.0
    async def active(_s=None): return []
    monkeypatch.setattr(kucoin_client, "get_symbol_info", info)
    monkeypatch.setattr(kucoin_client, "get_available_balance", bal)
    monkeypatch.setattr(kucoin_client, "list_active_orders", active)
    # notional 100 > available 10 -> rejected when live
    with pytest.raises(execution.ValidationError):
        asyncio.run(execution.validate_buy("BTC-USDT", 1.0, 100.0, live=True))
    # paper mode does not check real funds -> allowed
    assert asyncio.run(execution.validate_buy("BTC-USDT", 1.0, 100.0, live=False)) == pytest.approx(1.0)


def test_validate_blocks_duplicate_position(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DB_PATH", str(tmp_path / "s.sqlite3"))
    state.init_db()
    state.add_position(state.Position("BTC-USDT", "buy", 1, 100, 90, 120, 0.0, "o1"))
    async def info(_s): return _symbol_info()
    monkeypatch.setattr(kucoin_client, "get_symbol_info", info)
    with pytest.raises(execution.ValidationError):
        asyncio.run(execution.validate_buy("BTC-USDT", 1.0, 100.0, live=False))


def test_open_long_paper_is_simulated_no_real_order(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DB_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.setattr(config, "LIVE_TRADING", False)   # gate closed
    state.init_db()
    async def info(_s): return _symbol_info()
    monkeypatch.setattr(kucoin_client, "get_symbol_info", info)
    _id, res = asyncio.run(execution.open_long("BTC-USDT", 1.0, 100.0, 90.0, 120.0, live=False))
    assert res["dryRun"] is True             # place_market_order refused to go live
    assert state.count_open() == 1           # but the paper position is tracked


# ------------------------------------------------------------- the live gate
def test_live_gate_requires_both_switch_and_token(monkeypatch, tmp_path):
    token = str(tmp_path / ".golive")
    monkeypatch.setattr(config, "GOLIVE_TOKEN_PATH", token)

    monkeypatch.setattr(config, "LIVE_TRADING", False)
    assert preflight.live_allowed()[0] is False           # switch off

    monkeypatch.setattr(config, "LIVE_TRADING", True)
    assert preflight.live_allowed()[0] is False           # on, but not confirmed

    with open(token, "w") as fh:
        fh.write("confirmed")
    assert preflight.live_allowed()[0] is True            # both -> armed
