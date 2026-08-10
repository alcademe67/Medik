"""Tests for the ETF core-holding sizing path (strategy/risk.size_core_holding)
and the whitelist that gates it (strategy/core_holdings).

Run: python -m pytest tests/test_core_holdings.py -q
  or: python tests/test_core_holdings.py
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from strategy.config import StrategyConfig
from strategy.core_holdings import NotACoreETF, assert_core_etf, is_core_etf
from strategy.risk import RiskRejected, size_core_holding

CFG = StrategyConfig()


# --- whitelist ------------------------------------------------------------

def test_qqq_is_approved():
    assert is_core_etf("QQQ")
    assert assert_core_etf("qqq") == "QQQ"
    assert assert_core_etf("  QQQ  ") == "QQQ"


@pytest.mark.parametrize("sym", ["TQQQ", "SQQQ", "QLD", "QID", "PSQ", "UVXY", "SOXL"])
def test_leveraged_and_inverse_etfs_are_rejected_by_name(sym):
    """The whole point of the whitelist: these are ETFs too."""
    assert not is_core_etf(sym)
    with pytest.raises(NotACoreETF, match="leveraged or inverse"):
        assert_core_etf(sym)


@pytest.mark.parametrize("sym", ["AAPL", "F", "JPM", "NVDA"])
def test_single_stocks_are_rejected(sym):
    assert not is_core_etf(sym)
    with pytest.raises(NotACoreETF, match="not an approved core ETF"):
        assert_core_etf(sym)


def test_unknown_leveraged_product_still_rejected():
    """A leveraged ticker missing from KNOWN_LEVERAGED_OR_INVERSE must still
    be refused -- the whitelist, not the denylist, is the real protection."""
    with pytest.raises(NotACoreETF, match="not an approved core ETF"):
        assert_core_etf("ZZZL")


def test_size_core_holding_refuses_non_whitelisted_symbol():
    with pytest.raises(NotACoreETF):
        size_core_holding("TQQQ", 100.0, 1000.0, 1000.0, 0.0, CFG)


# --- which cap binds ------------------------------------------------------

def test_etf_notional_cap_binds_when_portfolio_is_empty():
    """No existing positions: 70% of funds is the constraint, not headroom."""
    plan = size_core_holding("QQQ", 100.0, 1000.0, 1000.0, 0.0, CFG)
    assert plan.binding_cap == "etf_notional"
    assert plan.position_value == pytest.approx(700.0)
    assert plan.pct_of_available_funds == pytest.approx(0.70)


def test_portfolio_headroom_binds_when_other_positions_exist():
    """80% deployed cap bites before the 70% ETF cap does."""
    # equity 1000 -> max deployed 800; 600 already invested -> 200 headroom.
    # 70% of 400 cash = 280, which is more than the 200 of headroom.
    plan = size_core_holding("QQQ", 100.0, 400.0, 1000.0, 600.0, CFG)
    assert plan.binding_cap == "portfolio_headroom"
    assert plan.position_value == pytest.approx(200.0)
    # and the 80% ceiling is exactly respected, not exceeded
    assert 600.0 + plan.position_value == pytest.approx(800.0)


def test_rejects_when_portfolio_already_at_deployed_cap():
    with pytest.raises(RiskRejected, match="deployed cap"):
        size_core_holding("QQQ", 100.0, 500.0, 1000.0, 800.0, CFG)


def test_rejects_when_over_deployed_cap():
    with pytest.raises(RiskRejected, match="deployed cap"):
        size_core_holding("QQQ", 100.0, 500.0, 1000.0, 950.0, CFG)


# --- caps are never exceeded by rounding ---------------------------------

def test_fractional_flooring_never_exceeds_the_cap():
    """Awkward price that doesn't divide evenly -- floor, never round up."""
    plan = size_core_holding("QQQ", 720.34, 138.39, 298.64, 0.0, CFG)
    cap = 138.39 * 0.70
    assert plan.position_value <= cap + 1e-9
    assert plan.quantity == pytest.approx(int(cap / 720.34 * 1_000_000) / 1_000_000)


def test_whole_share_mode_floors_to_integer():
    plan = size_core_holding("QQQ", 100.0, 1000.0, 1000.0, 0.0, CFG, fractional=False)
    assert plan.quantity == 7.0
    assert float(plan.quantity).is_integer()


def test_rejects_when_whole_share_unaffordable():
    """$700 of headroom can't buy one $720 share without fractional."""
    with pytest.raises(RiskRejected, match="position too small"):
        size_core_holding("QQQ", 720.34, 1000.0, 1000.0, 0.0, CFG, fractional=False)


def test_fractional_makes_the_same_position_viable():
    plan = size_core_holding("QQQ", 720.34, 1000.0, 1000.0, 0.0, CFG, fractional=True)
    assert plan.quantity > 0
    assert plan.position_value == pytest.approx(700.0, abs=0.01)


# --- input validation -----------------------------------------------------

def test_rejects_no_available_funds():
    with pytest.raises(RiskRejected, match="no available funds"):
        size_core_holding("QQQ", 100.0, 0.0, 1000.0, 0.0, CFG)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"price": 0.0},
        {"price": -5.0},
        {"net_liquidation": 0.0},
        {"gross_position_value": -1.0},
    ],
)
def test_rejects_invalid_inputs(kwargs):
    args = dict(
        symbol="QQQ", price=100.0, available_funds=1000.0,
        net_liquidation=1000.0, gross_position_value=0.0, config=CFG,
    )
    args.update(kwargs)
    with pytest.raises(ValueError):
        size_core_holding(**args)


# --- the live account, as of 2026-08-05 ----------------------------------

def test_live_account_scenario_headroom_binds():
    """Real numbers: $298.64 equity, $138.39 cash, $160.19 already in F+JPM.
    70% of cash = $96.87, but only $78.72 of headroom remains under the 80%
    cap -- so the deployed cap binds and the QQQ position is the smaller."""
    plan = size_core_holding("QQQ", 720.34, 138.39, 298.6387, 160.194, CFG)
    assert plan.binding_cap == "portfolio_headroom"
    assert plan.position_value == pytest.approx(78.72, abs=0.05)
    assert 160.194 + plan.position_value <= 0.80 * 298.6387 + 1e-9


def test_live_account_scenario_after_liquidating_legacy_positions():
    """If F and JPM were closed, cash ~= equity and the 70% ETF cap binds."""
    plan = size_core_holding("QQQ", 720.34, 296.32, 296.32, 0.0, CFG)
    assert plan.binding_cap == "etf_notional"
    assert plan.position_value == pytest.approx(207.42, abs=0.05)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
