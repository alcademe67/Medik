"""The quote path: streaming instead of snapshot, and what counts as usable.

IBKR sells streaming and snapshot quotes as separate entitlements. This
account holds streaming ("US Real-Time Non-Consolidated Streaming Quotes",
fee waived) but not snapshot, so ib.reqTickers() -- which is
reqMktData(..., snapshot=True) -- returned error 10089 for every US ETF while
TWS showed a live subscription. Confirmed 2026-08-24 with the probe.
"""
from __future__ import annotations

import importlib.util
import sys
import types

import pytest


def _live():
    if "ib_async" not in sys.modules:
        stub = types.ModuleType("ib_async")
        for n in ("IB", "Stock", "LimitOrder", "MarketOrder", "Trade"):
            setattr(stub, n, type(n, (), {}))
        sys.modules["ib_async"] = stub
    spec = importlib.util.spec_from_file_location(
        "etf_live_quotes", "examples/medik_etf_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NAN = float("nan")


class Ticker:
    def __init__(self, bid=70.20, ask=70.22, last=70.21, close=69.90,
                 marketDataType=1):
        self.bid, self.ask, self.last, self.close = bid, ask, last, close
        self.marketDataType = marketDataType


class Contract:
    def __init__(self, symbol):
        self.symbol = symbol


class FakeIB:
    def __init__(self, ticker=None):
        self.requested, self.cancelled, self.slept = [], [], []
        self._ticker = ticker or Ticker()

    def reqMktData(self, contract, generic, snapshot, regulatory):
        self.requested.append((contract.symbol, generic, snapshot, regulatory))
        return self._ticker

    def cancelMktData(self, contract):
        self.cancelled.append(contract.symbol)

    def sleep(self, sec):
        self.slept.append(sec)


# ------------------------------------------------------ usable_price


@pytest.mark.parametrize("bad", [NAN, -1.0, -1, 0, 0.0, None, "", "abc"])
def test_no_value_markers_become_zero(bad):
    assert _live().usable_price(bad) == 0.0


def test_a_real_price_survives():
    assert _live().usable_price(70.21) == pytest.approx(70.21)
    assert _live().usable_price("70.21") == pytest.approx(70.21)


def test_the_or_zero_idiom_would_not_have_caught_nan():
    """Why this helper exists: NaN is truthy, so `NaN or 0` is NaN, and the
    NaN then makes every downstream comparison silently False."""
    assert (NAN or 0) != 0          # the old code's assumption was wrong
    assert _live().usable_price(NAN) == 0.0


# --------------------------------------------------------- read_quote


def test_a_good_quote_is_returned():
    bid, ask, last, problem = _live().read_quote(Ticker())
    assert problem == ""
    assert (bid, ask, last) == pytest.approx((70.20, 70.22, 70.21))


def test_last_falls_back_to_close_then_to_the_bar():
    live = _live()
    _, _, last, problem = live.read_quote(Ticker(last=NAN))
    assert problem == "" and last == pytest.approx(69.90)
    _, _, last, problem = live.read_quote(Ticker(last=NAN, close=NAN), 68.5)
    assert problem == "" and last == pytest.approx(68.5)


def test_a_missing_bid_or_ask_is_refused():
    live = _live()
    assert "no usable quote" in live.read_quote(Ticker(bid=NAN))[3]
    assert "no usable quote" in live.read_quote(Ticker(ask=NAN))[3]


def test_no_price_at_all_is_refused():
    problem = _live().read_quote(Ticker(NAN, NAN, NAN, NAN), 0.0)[3]
    assert "no usable quote" in problem


def test_a_crossed_quote_is_refused():
    """bid above ask is a data glitch, not an arbitrage."""
    bid, ask, last, problem = _live().read_quote(Ticker(bid=70.30, ask=70.20))
    assert "crossed quote" in problem
    assert (bid, ask, last) == (0.0, 0.0, 0.0)


@pytest.mark.parametrize("md_type", [3, 4])
def test_delayed_data_is_refused_even_when_prices_look_fine(md_type):
    """The dangerous case: IBKR serves 15-minute-old prices with no error.

    A 5-minute strategy would keep trading, sizing stops off a quote three
    bars stale. Refusing costs a missed trade; accepting costs a wrong one.
    """
    bid, ask, last, problem = _live().read_quote(Ticker(marketDataType=md_type))
    assert "DELAYED" in problem
    assert (bid, ask, last) == (0.0, 0.0, 0.0)


@pytest.mark.parametrize("md_type", [1, 2])
def test_realtime_and_frozen_are_accepted(md_type):
    """Frozen (2) is the last live print, not a delayed feed — usable when the
    market is closed and honest about what it is."""
    assert _live().read_quote(Ticker(marketDataType=md_type))[3] == ""


def test_a_ticker_without_the_field_is_assumed_live():
    class Bare:
        bid, ask, last, close = 70.20, 70.22, 70.21, 69.90
    assert _live().read_quote(Bare())[3] == ""


# ---------------------------------------------------------- QuoteFeed


def test_the_request_is_streaming_not_snapshot():
    """The whole fix: snapshot=True is the entitlement this account lacks."""
    live = _live()
    ib = FakeIB()
    live.QuoteFeed(ib, warmup_sec=0).quote(Contract("TQQQ"))
    [(symbol, generic, snapshot, regulatory)] = ib.requested
    assert symbol == "TQQQ"
    assert snapshot is False
    assert regulatory is False
    assert generic == "", "generic ticks can need their own subscriptions"


def test_a_symbol_is_subscribed_once_and_reused():
    """Re-subscribing every cycle would waste a market-data line and re-pay
    the warm-up wait for twenty symbols every five minutes."""
    live = _live()
    ib = FakeIB()
    feed = live.QuoteFeed(ib, warmup_sec=3)
    for _ in range(5):
        feed.quote(Contract("TQQQ"))
    assert len(ib.requested) == 1
    assert ib.slept == [3], "only the first read waits"


def test_each_symbol_gets_its_own_subscription():
    live = _live()
    ib = FakeIB()
    feed = live.QuoteFeed(ib, warmup_sec=0)
    for sym in ("TQQQ", "SOXL", "SPY"):
        feed.quote(Contract(sym))
    assert [r[0] for r in ib.requested] == ["TQQQ", "SOXL", "SPY"]


def test_cancel_all_releases_every_line():
    live = _live()
    ib = FakeIB()
    feed = live.QuoteFeed(ib, warmup_sec=0)
    for sym in ("TQQQ", "SOXL"):
        feed.quote(Contract(sym))
    assert feed.cancel_all() == 2
    assert sorted(ib.cancelled) == ["SOXL", "TQQQ"]
    assert feed.cancel_all() == 0, "must be safe to call twice"


def test_a_failing_cancel_does_not_strand_the_others():
    live = _live()

    class Grumpy(FakeIB):
        def cancelMktData(self, contract):
            if contract.symbol == "TQQQ":
                raise RuntimeError("no such subscription")
            super().cancelMktData(contract)

    ib = Grumpy()
    feed = live.QuoteFeed(ib, warmup_sec=0)
    for sym in ("TQQQ", "SOXL"):
        feed.quote(Contract(sym))
    feed.cancel_all()
    assert ib.cancelled == ["SOXL"]


def test_resubscribing_after_cancel_works():
    live = _live()
    ib = FakeIB()
    feed = live.QuoteFeed(ib, warmup_sec=0)
    feed.quote(Contract("TQQQ"))
    feed.cancel_all()
    feed.quote(Contract("TQQQ"))
    assert len(ib.requested) == 2


# ------------------------------------------------- the runner as source


def test_the_runner_no_longer_calls_reqtickers():
    src = open("examples/medik_etf_live.py").read()
    code = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("#") and "log(" not in l)
    assert "reqTickers" not in code, \
        "reqTickers is snapshot=True — the entitlement this account lacks"
    assert "reqMktData" in code


def test_the_market_data_type_is_requested_explicitly():
    src = open("examples/medik_etf_live.py").read()
    assert "ib.reqMarketDataType(LIVE_MARKET_DATA_TYPE)" in src


def test_subscriptions_are_released_on_exit():
    src = open("examples/medik_etf_live.py").read()
    tail = src.split("    finally:")[-1]
    assert "feed.cancel_all()" in tail
