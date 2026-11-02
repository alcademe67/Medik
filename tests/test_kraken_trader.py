"""Offline tests for .claude/skills/kraken-trader/kraken.py (no network, no keys)."""
import importlib.util
import io
import json
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "kraken-trader" / "kraken.py"


@pytest.fixture
def k(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("kraken_trader", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("KRAKEN_TRADER_DIR", str(tmp_path))
    monkeypatch.setenv("KRAKEN_TRADER_NOW", "2026-11-02T15:00:00")
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    mod._PAIRS_CACHE = {"XXBTZUSD": {"altname": "XBTUSD", "wsname": "XBT/USD", "base": "XXBT",
                                     "quote": "ZUSD", "pair_decimals": 1, "lot_decimals": 8,
                                     "ordermin": "0.00005", "costmin": "0.5", "status": "online"}}

    def no_network(*a, **kw):
        raise AssertionError("network call in an offline test")
    mod._http = no_network
    return mod


def bar(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 1.0}


def test_signature_matches_kraken_documented_example(k):
    secret = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
    data = "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25"
    assert k.sign("/0/private/AddOrder", data, "1616492376594", secret) == (
        "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ==")


def test_ohlc_drops_the_forming_bar(k):
    rows = [[i, "1", "2", "0.5", "1.5", "1.2", "10", 3] for i in range(5)]
    k._http = lambda *a, **kw: {"error": [], "result": {"XXBTZUSD": rows, "last": 4}}
    bars = k.ohlc("BTCUSD", 60)
    assert [b["t"] for b in bars] == [0, 1, 2, 3]


def test_pair_aliases_resolve(k):
    for name in ("BTCUSD", "XBTUSD", "BTC/USD", "XXBTZUSD", "xbt/usd"):
        assert k.pair_info(name)[0] == "XXBTZUSD"


def test_target_is_net_of_fees(k):
    f, entry, stop = 0.004, 100.0, 95.0
    t = k.target_for_r(entry, stop, f, 2.0)
    assert k.net_reward(entry, t, f) / k.net_risk(entry, stop, f) == pytest.approx(2.0)
    assert (t - entry) / (entry - stop) > 2.0  # gross R must exceed the net R


def test_plan_refuses_stop_inside_fees(k):
    info = k._PAIRS_CACHE["XXBTZUSD"]
    with pytest.raises(k.KrakenError, match="Too tight"):
        k.make_plan("XXBTZUSD", info, 100.0, None, None, None, "pct:0.5", 2.0, 0.004)


def test_history_fills_next_open_and_is_stop_first(k):
    bars = [bar(i, 100, 101, 99, 100) for i in range(5)]
    bars.append(bar(5, 100, 103, 100, 102.5))       # closes above the 5-bar high 101 -> signal
    bars.append(bar(6, 102, 200, 50, 102))          # both stop and target touched -> stop counts
    bars += [bar(7 + i, 102, 102, 102, 102) for i in range(3)]
    trades = k.history(bars, 5, "pct:3", 2.0, 0.004, 10)
    assert trades[0]["entry"] == 102              # next bar's open, not the signal close
    assert trades[0]["how"] == "stop"
    assert trades[0]["r_net"] == pytest.approx(-1.0)


def setup_plan(k, account_value=1000.0):
    k.write_json("config.json", {"account_value": account_value, "cash": account_value})
    info = k._PAIRS_CACHE["XXBTZUSD"]
    p = k.make_plan("XXBTZUSD", info, 70000.0, None, None, None, "pct:3", 2.0, 0.004, 0.3)
    p.update({"interval": 60, "lookback": 20, "horizon_bars": 24})
    k.write_json("plan.json", p)
    return p


def test_size_respects_max_loss_and_min_order(k, capsys):
    setup_plan(k)
    assert k.main(["size"]) == 0
    item = k.read_json("orders.json")["orders"][0]
    assert item["meta"]["max_loss"] <= 10.0 + 1e-9      # 1% of 1000
    assert "leverage" not in item["order"]
    assert item["order"]["close[ordertype]"] == "stop-loss"


def test_size_refuses_below_kraken_minimum(k, capsys):
    setup_plan(k, account_value=5.0)
    assert k.main(["size"]) == 2
    assert "REFUSED" in capsys.readouterr().out


def test_risk_pass_then_edited_order_is_not_ready(k, capsys):
    setup_plan(k)
    k.main(["size"])
    assert k.main(["risk"]) == 0
    assert k.ready(1)[2] is True
    doc = k.read_json("orders.json")
    doc["orders"][0]["order"]["volume"] = "1.00000000"
    k.write_json("orders.json", doc)
    assert k.ready(1)[2] is False                    # fingerprint changed
    assert k.main(["risk"]) == 1                     # and the bigger order is blocked
    out = capsys.readouterr().out
    assert "loss:" in out and "position:" in out


def test_risk_blocks_entry_without_stop_and_short_sell(k, capsys):
    setup_plan(k)
    k.write_json("orders.json", {"orders": [
        {"order": {"pair": "XXBTZUSD", "type": "buy", "ordertype": "limit", "price": "70000",
                   "volume": "0.0001"}, "meta": {"target": 80000}},
        {"order": {"pair": "XXBTZUSD", "type": "sell", "ordertype": "limit", "price": "70000",
                   "volume": "0.5"}, "meta": {"exit": True}}]})
    assert k.main(["risk"]) == 1
    out = capsys.readouterr().out
    assert "stop: every entry needs" in out and "short:" in out


@pytest.mark.parametrize("reply,ok", [("yes", True), ("Yes.", True), ("y", True), ("sure", False),
                                      ("yes to all", False), ("yes ETH", False), ("ok go", False),
                                      ("yes BTC", True), ("yes XBT/USD", True)])
def test_approve_needs_explicit_yes(k, reply, ok):
    setup_plan(k)
    k.main(["size"])
    k.main(["risk"])
    assert (k.main(["approve", "--index", "1", "--reply", reply]) == 0) is ok


class FakeStdin(io.StringIO):
    def __init__(self, text, tty):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty


def test_place_refuses_without_approval_or_terminal(k, monkeypatch):
    setup_plan(k)
    k.main(["size"])
    k.main(["risk"])
    args = type("A", (), {"index": 1})()
    with pytest.raises(k.KrakenError, match="no recorded yes"):
        k.cmd_place(args, k.load_config(), stdin=FakeStdin("YES\n", True))
    k.main(["approve", "--index", "1", "--reply", "yes"])
    with pytest.raises(k.KrakenError, match="interactive terminal"):
        k.cmd_place(args, k.load_config(), stdin=FakeStdin("YES\n", False))


def test_place_validates_first_and_needs_typed_YES(k, monkeypatch):
    setup_plan(k)
    k.main(["size"])
    k.main(["risk"])
    k.main(["approve", "--index", "1", "--reply", "yes"])
    calls = []

    def fake_private(endpoint, params=None):
        calls.append(dict(params))
        return {"descr": {"order": "buy ..."}, "txid": ["OABC"]}
    k.private = fake_private
    args = type("A", (), {"index": 1})()
    assert k.cmd_place(args, k.load_config(), stdin=FakeStdin("yes\n", True)) == 1   # lowercase != YES
    assert len(calls) == 1 and calls[0]["validate"] == "true"
    calls.clear()
    assert k.cmd_place(args, k.load_config(), stdin=FakeStdin("YES\n", True)) == 0
    assert calls[0]["validate"] == "true" and "validate" not in calls[1]
    assert k.broker_log()["entries"][-1]["txid"] == ["OABC"]


def test_daily_limits_count_placed_entries(k, capsys):
    setup_plan(k)
    k.write_json("broker.json", {"entries": [
        {"kind": "order", "status": "placed", "date": "2026-11-02", "pair": "XETHZUSD", "max_loss": 9.0},
        {"kind": "order", "status": "placed", "date": "2026-11-02", "pair": "XXRPZUSD", "max_loss": 9.0},
        {"kind": "order", "status": "placed", "date": "2026-11-02", "pair": "SOLUSD", "max_loss": 9.0}]})
    k.main(["size"])
    assert k.main(["risk"]) == 1
    out = capsys.readouterr().out
    assert "daily loss" in out and "trades:" in out
