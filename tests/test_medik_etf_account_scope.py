"""The two-managed-account failure: balances read $0.00 for a funded account.

IBKR reports every account value keyed by (account, tag, currency). Reducing
that to {tag: value} collapses two accounts into whichever row happened to
arrive last, so a funded account is silently read with an empty account's
numbers. These tests pin the scoping rather than the symptom.
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
        "etf_live_scope", "examples/medik_etf_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LIVE_ACCT = "U26953060"
OTHER_ACCT = "U26920266"


class _Val:
    def __init__(self, account, tag, value, currency="USD"):
        self.account, self.tag, self.value, self.currency = account, tag, value, currency


class _Contract:
    def __init__(self, symbol):
        self.symbol = symbol


class _PortfolioItem:
    def __init__(self, account, symbol, position, marketValue, averageCost):
        self.account, self.contract = account, _Contract(symbol)
        self.position, self.marketValue, self.averageCost = position, marketValue, averageCost


class _PositionItem:
    def __init__(self, account, symbol, position, avgCost):
        self.account, self.contract = account, _Contract(symbol)
        self.position, self.avgCost = position, avgCost


class _Order:
    def __init__(self, action="SELL", account=""):
        self.action, self.account = action, account


class _Status:
    def __init__(self, status):
        self.status = status


class _Trade:
    def __init__(self, symbol, status, account="", action="SELL"):
        self.contract, self.orderStatus = _Contract(symbol), _Status(status)
        self.order = _Order(action, account)


class FakeIB:
    """Reproduces the real login: two accounts, only one of them funded."""

    def __init__(self, values=None, portfolio=(), positions=(), trades=()):
        self._values = values if values is not None else [
            # The empty account is listed LAST on purpose: that ordering is
            # what made {tag: value} report $0.00 for a funded account.
            _Val(LIVE_ACCT, "NetLiquidation", "286.15"),
            _Val(LIVE_ACCT, "AvailableFunds", "286.15"),
            _Val(OTHER_ACCT, "NetLiquidation", "0.00"),
            _Val(OTHER_ACCT, "AvailableFunds", "0.00"),
        ]
        self._portfolio, self._positions, self._trades = portfolio, positions, trades
        self.subscribed = []

    def accountValues(self, account=""):
        return [v for v in self._values if not account or v.account == account]

    def portfolio(self, account=""):
        return list(self._portfolio)

    def positions(self, account=""):
        return list(self._positions)

    def openTrades(self):
        return list(self._trades)

    def reqAccountUpdates(self, account=""):
        self.subscribed.append(account)

    def sleep(self, _):
        pass


# ------------------------------------------------------- the reported bug


def test_the_funded_account_is_read_not_the_empty_one():
    """The regression: this returned $0.00 before scoping."""
    state = _live().read_portfolio(FakeIB(), LIVE_ACCT)
    assert state.net_liquidation == pytest.approx(286.15)
    assert state.available_cash == pytest.approx(286.15)


def test_ordering_cannot_change_the_answer():
    """Whichever account TWS lists last must not matter."""
    ib = FakeIB()
    ib._values.reverse()
    assert _live().read_portfolio(ib, LIVE_ACCT).net_liquidation == pytest.approx(286.15)


def test_reading_the_other_account_gives_its_own_zero():
    """Scoping is real, not a hard-coded preference for the funded account."""
    assert _live().read_portfolio(FakeIB(), OTHER_ACCT).net_liquidation == 0.0


def test_an_unscoped_read_is_still_possible_for_single_account_logins():
    ib = FakeIB(values=[_Val(LIVE_ACCT, "NetLiquidation", "286.15")])
    assert _live().read_portfolio(ib, "").net_liquidation == pytest.approx(286.15)


# ------------------------------------------------------ positions scoping


def test_another_accounts_position_is_not_adopted_as_ours():
    ib = FakeIB(portfolio=[_PortfolioItem(OTHER_ACCT, "AAPL", 10, 2000.0, 190.0)])
    assert _live().read_portfolio(ib, LIVE_ACCT).positions == ()


def test_our_own_position_is_read_with_live_value_and_basis():
    ib = FakeIB(portfolio=[_PortfolioItem(LIVE_ACCT, "TQQQ", 4, 280.0, 68.0)])
    [pos] = _live().read_portfolio(ib, LIVE_ACCT).positions
    assert pos.market_value == pytest.approx(280.0)   # live value, not 4*68
    assert pos.avg_cost == pytest.approx(68.0)


def test_the_positions_fallback_is_also_scoped():
    ib = FakeIB(positions=[_PositionItem(OTHER_ACCT, "AAPL", 10, 190.0),
                           _PositionItem(LIVE_ACCT, "TQQQ", 4, 68.0)])
    [pos] = _live().read_portfolio(ib, LIVE_ACCT).positions
    assert pos.symbol == "TQQQ"


# --------------------------------------------------------- order scoping


def test_another_accounts_working_order_does_not_block_our_entries():
    ib = FakeIB(trades=[_Trade("AAPL", "Submitted", OTHER_ACCT)])
    assert _live().read_portfolio(ib, LIVE_ACCT).open_order_count == 0


def test_our_own_working_order_blocks():
    ib = FakeIB(trades=[_Trade("TQQQ", "Submitted", LIVE_ACCT)])
    assert _live().read_portfolio(ib, LIVE_ACCT).open_order_count == 1


def test_an_untagged_order_is_counted_as_blocking():
    """Blank account is ambiguous. Over-counting stops trading; under-counting
    can put a second entry out beside a live one."""
    ib = FakeIB(trades=[_Trade("TQQQ", "Submitted", "")])
    assert _live().read_portfolio(ib, LIVE_ACCT).open_order_count == 1


def test_apicancelled_no_longer_counts_as_a_live_order():
    """It is terminal, and counting it silently disabled entries for the run."""
    ib = FakeIB(trades=[_Trade("TQQQ", "ApiCancelled", LIVE_ACCT)])
    assert _live().read_portfolio(ib, LIVE_ACCT).open_order_count == 0


def test_inactive_is_deliberately_still_counted():
    """IBKR also uses Inactive for an accepted-but-not-yet-working order."""
    ib = FakeIB(trades=[_Trade("TQQQ", "Inactive", LIVE_ACCT)])
    assert _live().read_portfolio(ib, LIVE_ACCT).open_order_count == 1


# ------------------------------------------------------ account selection


def test_the_env_var_selects_the_account(monkeypatch):
    live = _live()
    monkeypatch.setenv(live.ACCOUNT_ENV_VAR, LIVE_ACCT)
    account, why = live.resolve_account([OTHER_ACCT, LIVE_ACCT])
    assert account == LIVE_ACCT and live.ACCOUNT_ENV_VAR in why


def test_two_accounts_without_the_env_var_refuse(monkeypatch):
    live = _live()
    monkeypatch.delenv(live.ACCOUNT_ENV_VAR, raising=False)
    account, why = live.resolve_account([OTHER_ACCT, LIVE_ACCT])
    assert account == "" and "refusing to guess" in why


def test_a_typo_in_the_account_id_refuses(monkeypatch):
    live = _live()
    monkeypatch.setenv(live.ACCOUNT_ENV_VAR, "U2695306")      # one digit short
    account, why = live.resolve_account([OTHER_ACCT, LIVE_ACCT])
    assert account == "" and "not one of the managed accounts" in why


def test_a_single_account_login_needs_no_env_var(monkeypatch):
    live = _live()
    monkeypatch.delenv(live.ACCOUNT_ENV_VAR, raising=False)
    account, why = live.resolve_account([LIVE_ACCT])
    assert account == LIVE_ACCT and "only managed account" in why


def test_no_accounts_refuses(monkeypatch):
    live = _live()
    monkeypatch.delenv(live.ACCOUNT_ENV_VAR, raising=False)
    assert live.resolve_account([])[0] == ""


# ----------------------------------------------------- explicit subscription


def test_subscribe_asks_for_the_named_account():
    live, ib = _live(), FakeIB()
    assert live.subscribe_account(ib, LIVE_ACCT) is True
    assert ib.subscribed == [LIVE_ACCT]


def test_subscribe_reports_failure_when_no_value_arrives():
    """A missing subscription must surface as False, never as $0.00 equity."""
    live, ib = _live(), FakeIB(values=[])
    assert live.subscribe_account(ib, LIVE_ACCT, timeout=0.3) is False


def test_a_zero_valued_tag_does_not_count_as_subscribed():
    live = _live()
    ib = FakeIB(values=[_Val(LIVE_ACCT, "NetLiquidation", "")])
    assert live.subscribe_account(ib, LIVE_ACCT, timeout=0.3) is False


# -------------------------------------------------- orders carry the account


def test_every_bracket_leg_is_tagged_with_the_account():
    """An untagged order on a multi-account login can be routed to the wrong
    account by TWS, or rejected outright."""
    src = open("examples/medik_etf_live.py").read()
    body = src.split("def place_bracket")[1].split("\ndef ")[0]
    assert "leg.account = account" in body
    assert body.index("leg.account = account") < body.index("ib.placeOrder")


def test_the_emergency_flatten_is_tagged_too():
    src = open("examples/medik_etf_live.py").read()
    body = src.split("def flatten")[1].split("\ndef ")[0]
    assert "order.account = account" in body
    assert body.index("order.account = account") < body.index("ib.placeOrder")
