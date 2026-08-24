"""Account scoping for the scripts that read or sell across a whole login.

liquidate_positions.py --all iterates ib.positions(), which spans EVERY
account under the login. tws_status.py reduced account values to {tag: value},
which keeps whichever account's row arrived last. Neither was wrong until a
second account activated; both were then wrong silently.
"""
from __future__ import annotations

import pytest

from ibkr.accounts import belongs_to, order_belongs_to, resolve_account

LIVE = "U26953060"
OTHER = "U26920266"


class _Row:
    def __init__(self, account):
        self.account = account


class _Order:
    def __init__(self, account=""):
        self.account = account


class _Trade:
    def __init__(self, account=""):
        self.order = _Order(account)


# ------------------------------------------------------------- selection


def test_a_sole_account_needs_no_naming(monkeypatch):
    monkeypatch.delenv("IBKR_ACCOUNT", raising=False)
    account, why = resolve_account([LIVE])
    assert account == LIVE and "only managed account" in why


def test_two_accounts_refuse_rather_than_pick_the_first(monkeypatch):
    """The whole point: defaulting to managed[0] would sell from whichever
    account TWS happened to list first."""
    monkeypatch.delenv("IBKR_ACCOUNT", raising=False)
    account, why = resolve_account([OTHER, LIVE])
    assert account == "" and "refusing to guess" in why


def test_an_explicit_flag_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("IBKR_ACCOUNT", OTHER)
    account, why = resolve_account([OTHER, LIVE], explicit=LIVE)
    assert account == LIVE and "--account" in why


def test_the_environment_is_used_when_no_flag_is_given(monkeypatch):
    monkeypatch.setenv("IBKR_ACCOUNT", LIVE)
    assert resolve_account([OTHER, LIVE])[0] == LIVE


def test_env_vars_are_tried_in_order(monkeypatch):
    monkeypatch.setenv("MEDIK_ETF_ACCOUNT", LIVE)
    monkeypatch.setenv("IBKR_ACCOUNT", OTHER)
    account, why = resolve_account([OTHER, LIVE],
                                   env_vars=("MEDIK_ETF_ACCOUNT", "IBKR_ACCOUNT"))
    assert account == LIVE and "MEDIK_ETF_ACCOUNT" in why


def test_an_unknown_account_refuses_and_names_the_source(monkeypatch):
    monkeypatch.setenv("IBKR_ACCOUNT", "U0000000")
    account, why = resolve_account([OTHER, LIVE])
    assert account == "" and "IBKR_ACCOUNT" in why and "not one of" in why


def test_whitespace_is_not_an_account(monkeypatch):
    monkeypatch.delenv("IBKR_ACCOUNT", raising=False)
    assert resolve_account([OTHER, LIVE], explicit="   ")[0] == ""


def test_no_accounts_refuses(monkeypatch):
    monkeypatch.delenv("IBKR_ACCOUNT", raising=False)
    assert resolve_account([])[0] == ""
    assert resolve_account([""])[0] == ""


# -------------------------------------------------------------- filtering


def test_rows_are_matched_on_their_own_account_field():
    assert belongs_to(_Row(LIVE), LIVE) is True
    assert belongs_to(_Row(OTHER), LIVE) is False


def test_an_empty_selection_matches_everything():
    """Single-account callers that pass no account keep working."""
    assert belongs_to(_Row(OTHER), "") is True


def test_a_row_without_an_account_field_is_not_claimed():
    assert belongs_to(object(), LIVE) is False


def test_an_untagged_order_counts_as_ours():
    """Asymmetric on purpose: mistaking a stranger's order for ours only makes
    us cautious, while missing one of ours leaves it uncancelled."""
    assert order_belongs_to(_Trade(""), LIVE) is True
    assert order_belongs_to(_Trade(LIVE), LIVE) is True
    assert order_belongs_to(_Trade(OTHER), LIVE) is False


# ------------------------------------------- the two scripts, as source


def test_liquidate_refuses_to_run_without_a_resolved_account():
    src = open("examples/liquidate_positions.py").read()
    assert "resolve_account" in src
    assert "REFUSING TO RUN" in src
    # the refusal must come before anything is priced or placed
    assert src.index("REFUSING TO RUN") < src.index("place_limit_order_on_contract(\n")


def test_liquidate_filters_positions_by_account():
    src = open("examples/liquidate_positions.py").read()
    body = src.split("def main")[1]
    for line in body.splitlines():
        if "ib.positions()" in line or "in ib.positions()" in line:
            continue
    assert body.count("belongs_to(p, account)") >= 2, \
        "both the sell list and the after-report must be scoped"


def test_liquidate_tags_its_sell_orders_with_the_account():
    src = open("examples/liquidate_positions.py").read()
    call = src.split("place_limit_order_on_contract(")[1].split(")")[0]
    assert "account=account" in call


def test_the_order_helper_sets_the_account_on_the_order():
    import inspect

    from ibkr.orders import place_limit_order_on_contract
    src = inspect.getsource(place_limit_order_on_contract)
    assert "order.account = account" in src
    assert src.index("order.account = account") < src.index("ib.placeOrder")


def test_the_confirm_gate_is_still_required():
    """Adding an account argument must not have loosened the human gate."""
    from ibkr.orders import OrderRejected, place_limit_order_on_contract

    class _C:
        symbol = "F"

    with pytest.raises(OrderRejected):
        place_limit_order_on_contract(None, _C(), "SELL", 1, 13.5, account=LIVE)


def test_tws_status_reports_each_account_separately():
    src = open("examples/tws_status.py").read()
    assert "for acct in accounts:" in src
    assert "belongs_to(row, acct)" in src
    # the collapsing comprehension must be gone
    assert "for row in ib.accountValues()\n            if row.currency" not in src
