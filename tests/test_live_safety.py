"""Safety tests for the live-trading path.

Covers the risk manager, the pre-trade validation, the persistence layer,
and the live gate. All offline — network calls to KuCoin are monkeypatched.
These guard the properties that keep real money safe.
"""

import asyncio
import time

import pytest

from bot import config, execution, kucoin_client, preflight, risk, state
from bot.config import RiskParams


# ------------------------------------------------------------ risk manager
def test_position_size_risks_configured_fraction():
    # equity 10000, 1% risk = 100. entry 100, stop 90 => 10/unit => 10 units.
    size = risk.position_size(equity=10_000, entry_price=100, stop_price=90, available_quote=1e9)
    assert size == pytest.approx(10.0)


def test_position_size_capped_by_available_cash():
    # cash_buffer=0 isolates the raw affordability cap (available / price).
    p = RiskParams(cash_buffer=0.0)
    size = risk.position_size(equity=10_000, entry_price=100, stop_price=90,
                              available_quote=500, params=p)
    assert size == pytest.approx(5.0)  # can only afford 5 units at 100


def test_position_size_reserves_cash_so_order_fits_balance():
    # The 200004 fix: sizing to cash must NOT target 100% of the balance — a
    # slice is held back for the taker fee + slippage. With a 2% buffer, the
    # cash cap yields 4.9 units (notional 490 = 98% of 500), not 5.0 (100%).
    p = RiskParams(cash_buffer=0.02, max_position_pct=0, max_risk_per_trade=1.0)
    bd = risk.size_breakdown(equity=500, entry_price=100, stop_price=99,
                             available_quote=500, params=p)
    assert bd["size"] == pytest.approx(4.9)
    assert bd["size"] * 100 <= 500 * 0.98 + 1e-9      # notional stays under 98%


def test_position_size_zero_on_bad_inputs():
    assert risk.position_size(equity=0, entry_price=100, stop_price=90, available_quote=100) == 0
    assert risk.position_size(equity=100, entry_price=100, stop_price=100, available_quote=100) == 0


def test_size_breakdown_explains_why_size_is_zero_or_not():
    # No cash on hand -> affordable 0 -> size 0, reason names the cash constraint.
    bd = risk.size_breakdown(equity=1000, entry_price=100, stop_price=90, available_quote=0)
    assert bd["size"] == 0 and "buys 0 units" in bd["reason"]

    # Stop not below entry -> risk_per_unit <= 0 -> size 0 with a clear reason.
    bd2 = risk.size_breakdown(equity=1000, entry_price=100, stop_price=100, available_quote=1e9)
    assert bd2["size"] == 0 and "risk_per_unit" in bd2["reason"]

    # Healthy inputs -> positive size and a named binding constraint.
    bd3 = risk.size_breakdown(equity=10_000, entry_price=100, stop_price=90, available_quote=1e9)
    assert bd3["size"] > 0 and "binding constraint" in bd3["reason"]

    # Tiny balance is the binding constraint, and it's named as such. With the
    # default 2% cash reserve, 500 buys 4.9 units at 100 (not 5.0 = 100%).
    bd4 = risk.size_breakdown(equity=10_000, entry_price=100, stop_price=90, available_quote=500)
    assert bd4["size"] == pytest.approx(4.9) and "available cash" in bd4["reason"]


def test_position_size_capped_by_max_position_pct():
    # Tight 1% stop: risk sizing alone wants 100 units (100% of equity).
    # The 25% notional cap must clamp it to 25 units (25% of 10000 at price 100).
    p = RiskParams(max_risk_per_trade=0.01, max_position_pct=25)
    size = risk.position_size(
        equity=10_000, entry_price=100, stop_price=99, available_quote=1e9, params=p
    )
    assert size == pytest.approx(25.0)


def test_position_size_cap_disabled_when_zero():
    # max_position_pct=0 disables the cap -> full risk-based size returns.
    p = RiskParams(max_risk_per_trade=0.01, max_position_pct=0)
    size = risk.position_size(
        equity=10_000, entry_price=100, stop_price=99, available_quote=1e9, params=p
    )
    assert size == pytest.approx(100.0)


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


def test_check_can_trade_enforces_daily_order_cap():
    p = RiskParams(max_daily_orders=10)
    assert not risk.check_can_trade(
        open_positions=0, realized_pnl_today=0, start_equity_today=1000,
        consecutive_losses=0, orders_today=10, params=p
    ).allowed
    assert risk.check_can_trade(
        open_positions=0, realized_pnl_today=0, start_equity_today=1000,
        consecutive_losses=0, orders_today=9, params=p
    ).allowed


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


def test_orders_today_counts_open_and_closed_entries(tmp_path):
    db = str(tmp_path / "s.sqlite3")
    state.init_db(db)
    state.ensure_day(1000, db)
    now = time.time()
    # An entry opened today and still open counts...
    state.add_position(state.Position("BTC-USDT", "buy", 1, 100, 90, 120, now, "o1"), db)
    # ...and an entry opened today that we already closed still counts (can't
    # be reset by closing the trade — the daily cap must survive round-trips).
    pid = state.add_position(state.Position("ETH-USDT", "buy", 1, 100, 90, 120, now, "o2"), db)
    state.close_position(pid, 110, "target", db)
    assert state.orders_today(db) == 2
    # A position opened before today (epoch 0) does NOT count toward today.
    state.add_position(state.Position("SOL-USDT", "buy", 1, 100, 90, 120, 0.0, "old"), db)
    assert state.orders_today(db) == 2


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


def test_validate_buy_reserves_fee_room_prevents_200004(monkeypatch, tmp_path):
    # Root cause of KuCoin 200004: an order sized to 100% of the balance passes
    # a naive `notional <= available` check, but its real cost (notional + fee)
    # overruns the balance. The guard must require room for the fee too.
    monkeypatch.setattr(config, "STATE_DB_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.setattr(config, "FEE_RATE", 0.001)
    state.init_db()
    async def info(_s): return _symbol_info()
    async def bal(_c, account_type="trade"): return 100.0
    async def active(_s=None): return []
    monkeypatch.setattr(kucoin_client, "get_symbol_info", info)
    monkeypatch.setattr(kucoin_client, "get_available_balance", bal)
    monkeypatch.setattr(kucoin_client, "list_active_orders", active)
    # notional == available (100): naive check passed -> KuCoin 200004. Now rejected.
    with pytest.raises(execution.ValidationError):
        asyncio.run(execution.validate_buy("BTC-USDT", 1.0, 100.0, live=True))
    # A size leaving room for the fee is accepted (99*1.001 = 99.099 < 100).
    assert asyncio.run(execution.validate_buy("BTC-USDT", 0.99, 100.0, live=True)) == pytest.approx(0.99)


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


# ------------------------------------------------------ fills & fee logging
def test_get_order_fills_aggregates(monkeypatch):
    async def fake_signed_get(path, params=None):
        assert path == "/api/v1/fills"
        return {"items": [
            {"size": "0.5", "funds": "50", "fee": "0.05", "feeCurrency": "USDT"},
            {"size": "0.5", "funds": "52", "fee": "0.052", "feeCurrency": "USDT"},
        ]}
    monkeypatch.setattr(kucoin_client, "_signed_get", fake_signed_get)
    f = asyncio.run(kucoin_client.get_order_fills("order123"))
    assert f["filled_size"] == pytest.approx(1.0)
    assert f["avg_price"] == pytest.approx(102.0)      # 102 funds / 1.0 filled
    assert f["fee"] == pytest.approx(0.102)
    assert f["fee_currency"] == "USDT" and f["fills"] == 2


def test_settle_fees_paper_estimates_without_network(monkeypatch):
    called = False
    async def spy(_oid):
        nonlocal called
        called = True
        return {}
    monkeypatch.setattr(kucoin_client, "get_order_fills", spy)
    monkeypatch.setattr(config, "FEE_RATE", 0.001)
    # Paper: fee is ESTIMATED as notional * FEE_RATE; the fills endpoint is untouched.
    settle = asyncio.run(execution._settle_fees({"dryRun": True, "order": {}}, "BTC-USDT", "BUY", 250.0))
    assert called is False
    assert settle["fee"] == pytest.approx(0.25) and settle["filled_size"] == 0.0


def test_settle_fees_live_best_effort_zero_on_error(monkeypatch):
    async def boom(_oid):
        raise kucoin_client.KucoinError("fills endpoint down")
    monkeypatch.setattr(kucoin_client, "get_order_fills", boom)
    # The order already executed; a fills-lookup failure must NOT raise -> fee 0.
    settle = asyncio.run(execution._settle_fees({"dryRun": False, "orderId": "x"}, "BTC-USDT", "SELL", 100.0))
    assert settle["fee"] == 0.0


def test_settle_fees_converts_base_currency_fee_to_quote(monkeypatch):
    async def fills(_oid):
        return {"filled_size": 2.0, "avg_price": 100.0, "fee": 0.002,
                "fee_currency": "BTC", "fills": 1}
    monkeypatch.setattr(kucoin_client, "get_order_fills", fills)
    # A BUY fee charged in BTC is valued in USDT at the fill price: 0.002 * 100 = 0.20.
    settle = asyncio.run(execution._settle_fees({"dryRun": False, "orderId": "o"}, "BTC-USDT", "BUY", 200.0))
    assert settle["fee"] == pytest.approx(0.20)
    assert settle["filled_size"] == pytest.approx(2.0)


def test_open_long_live_buys_by_funds_and_records_filled_base(monkeypatch, tmp_path):
    # Option 2: a live BUY is placed by FUNDS (quote to spend), never by base
    # size — so it can't overrun the balance (KuCoin 200004). The position then
    # holds the ACTUAL filled base, conservatively net of the taker fee so the
    # later SELL can never try to offload more than we own.
    monkeypatch.setattr(config, "STATE_DB_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.setattr(config, "LIVE_TRADING", True)
    monkeypatch.setattr(config, "FEE_RATE", 0.001)
    state.init_db()
    captured = {}
    async def info(_s): return _symbol_info(base_increment=0.00000001)
    async def bal(_c, account_type="trade"): return 1000.0
    async def active(_s=None): return []
    async def place(symbol, side, *, funds=None, size=None):
        captured["funds"], captured["size"] = funds, size
        return {"dryRun": False, "orderId": "ORD-BUY", "order": {"clientOid": "c1"}}
    async def fills(_oid):
        return {"filled_size": 0.02, "avg_price": 100.0, "fee": 0.002,
                "fee_currency": "USDT", "fills": 1}
    monkeypatch.setattr(kucoin_client, "get_symbol_info", info)
    monkeypatch.setattr(kucoin_client, "get_available_balance", bal)
    monkeypatch.setattr(kucoin_client, "list_active_orders", active)
    monkeypatch.setattr(kucoin_client, "place_market_order", place)
    monkeypatch.setattr(kucoin_client, "get_order_fills", fills)

    asyncio.run(execution.open_long("BTC-USDT", 0.02, 100.0, 96.0, 108.0, live=True))
    # placed by funds (=0.02*100), NOT by base size
    assert captured["funds"] == pytest.approx(2.0) and captured["size"] is None
    pos = state.open_positions()[0]
    assert pos.size == pytest.approx(0.02 * (1 - 0.001))   # filled net of fee
    assert pos.entry_price == pytest.approx(100.0)         # actual fill price


# ------------------------------------------------------ candle history fetch
def test_fetch_ohlcv_requests_time_window_and_returns_enough(monkeypatch):
    # The regime signal needs ~200+ bars; without an explicit time window KuCoin
    # can return only a small recent slice -> permanent "warming-up". Assert the
    # fetch asks for a [startAt, endAt] window and returns the requested count.
    captured = {}

    def rows(n):  # KuCoin: newest-first [time, open, close, high, low, vol, turnover]
        base = 1_700_000_000
        return [[str(base - i * 300), "100", "101", "102", "99", "10", "0"] for i in range(n)]

    class FakeResp:
        status_code = 200
        def json(self):
            return {"code": "200000", "data": rows(500)}

    class FakeClient:
        async def get(self, path, params=None):
            captured["params"] = params
            return FakeResp()

    monkeypatch.setattr(kucoin_client, "_client", FakeClient())
    out = asyncio.run(kucoin_client.fetch_ohlcv("BTC-USDT", "5min", 400))
    assert "startAt" in captured["params"] and "endAt" in captured["params"]
    assert captured["params"]["endAt"] > captured["params"]["startAt"]
    assert len(out) == 400                      # trimmed to the requested limit
    assert set(out[0]) == {"time", "open", "close", "high", "low", "volume"}


# ------------------------------------------------- phantom / sell reconciliation
def test_close_long_aborts_and_removes_phantom_when_base_missing(monkeypatch, tmp_path):
    # Requirement 3: never SELL base the exchange doesn't hold. If it's missing,
    # remove the phantom position and submit no order.
    monkeypatch.setattr(config, "STATE_DB_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.setattr(config, "LIVE_TRADING", True)
    state.init_db()
    state.add_position(state.Position("BTC-USDT", "buy", 0.01, 100, 96, 108, 0.0, "o"))
    pos = state.open_positions()[0]

    placed = {"n": 0}
    async def bal(_c, account_type="trade"):
        return 0.0                       # exchange holds no BTC
    async def place(*_a, **_k):
        placed["n"] += 1
        return {"dryRun": False, "orderId": "x", "order": {}}
    monkeypatch.setattr(kucoin_client, "get_available_balance", bal)
    monkeypatch.setattr(kucoin_client, "place_market_order", place)

    net, res = asyncio.run(execution.close_long(pos, 108.0, "target"))
    assert res.get("phantom") is True
    assert placed["n"] == 0             # NO sell was submitted
    assert net == 0.0
    assert state.count_open() == 0      # phantom removed


# ------------------------------------------------------ net P/L accounting
def test_position_entry_fee_roundtrips(tmp_path):
    db = str(tmp_path / "s.sqlite3")
    state.init_db(db)
    state.add_position(
        state.Position("BTC-USDT", "buy", 1, 100, 96, 108, 0.0, "o", entry_fee=0.33), db
    )
    assert state.open_positions(db)[0].entry_fee == pytest.approx(0.33)


def test_close_position_records_net_pnl_after_fees(tmp_path):
    db = str(tmp_path / "s.sqlite3")
    state.init_db(db)
    state.ensure_day(1000, db)
    pid = state.add_position(
        state.Position("BTC-USDT", "buy", 2.0, 100, 96, 108, 0.0, "oid", entry_fee=0.20), db
    )
    # gross = (108-100)*2 = 16 ; fees = 0.20 + 0.27 = 0.47 ; net = 15.53
    net = state.close_position(pid, 108, "target", db, entry_fee=0.20, exit_fee=0.27)
    assert net == pytest.approx(15.53)
    assert state.today_realized_pnl(db) == pytest.approx(15.53)   # daily limit uses NET
    tr = state.recent_trades(1, db)[0]
    assert tr["pnl"] == pytest.approx(15.53)       # history stores NET
    assert tr["gross_pnl"] == pytest.approx(16.0)  # gross kept
    assert tr["fees"] == pytest.approx(0.47)       # fees kept
    st = state.stats(db)
    assert st["total_pnl"] == pytest.approx(15.53)
    assert st["gross_pnl"] == pytest.approx(16.0)
    assert st["total_fees"] == pytest.approx(0.47)


def test_fees_flip_a_gross_win_to_a_net_loss(tmp_path):
    db = str(tmp_path / "s.sqlite3")
    state.init_db(db)
    state.ensure_day(1000, db)
    # gross +0.2, fees 0.30 -> net -0.10: must count as a LOSS for streak & win-rate.
    pid = state.add_position(
        state.Position("BTC-USDT", "buy", 1.0, 100, 96, 101, 0.0, "o", entry_fee=0.10), db
    )
    net = state.close_position(pid, 100.2, "signal", db, entry_fee=0.10, exit_fee=0.20)
    assert net == pytest.approx(-0.10)
    assert state.consecutive_losses(db) == 1        # NET loss increments the streak
    assert state.stats(db)["win_rate"] == pytest.approx(0.0)   # gross-win but NET loss


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


def test_all_passed_ignores_advisory_symbol_checks():
    # One coin in a watchlist being untradable is advisory: it must NOT block
    # live trading (that coin is just skipped at order time). A REQUIRED check
    # failing still blocks.
    ok = [
        preflight.Check("KuCoin reachable", True, ""),
        preflight.Check("API keys valid (auth OK)", True, ""),
        preflight.Check("DOGE-USDT tradable", False, "halted", required=False),
    ]
    assert preflight.all_passed(ok) is True

    bad = [
        preflight.Check("KuCoin reachable", True, ""),
        preflight.Check("API keys valid (auth OK)", False, "rejected"),   # required
        preflight.Check("BTC-USDT tradable", True, "", required=False),
    ]
    assert preflight.all_passed(bad) is False

    # An all-advisory list has no required checks -> not "passed" (empty guard).
    assert preflight.all_passed([preflight.Check("x", True, "", required=False)]) is False
