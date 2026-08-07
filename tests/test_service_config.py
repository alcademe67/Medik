"""The scan loop must stay off unless it is switched on deliberately.

SCAN_ENABLED is opt-IN. These tests exist so a future edit can't quietly
flip the default back to "on": the pipeline runs the gate and pullback
strategies, both of which backtested to negative expectancy, against an
account whose adopted strategy is buy-and-hold.
"""
from __future__ import annotations

import pytest

from service.config import load_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("SCAN_ENABLED", "MODE", "IBKR_PORT"):
        monkeypatch.delenv(key, raising=False)


def test_scanning_is_off_when_unset():
    assert load_config().scan_enabled is False


@pytest.mark.parametrize("value", ["true", "TRUE", "True", " yes ", "1", "on"])
def test_explicit_opt_in_enables_scanning(monkeypatch, value):
    monkeypatch.setenv("SCAN_ENABLED", value)
    assert load_config().scan_enabled is True


@pytest.mark.parametrize(
    "value",
    [
        "false",
        "0",
        "no",
        "",
        "tru",       # typo must not enable
        "enabled",   # plausible-looking but not a recognized truthy value
    ],
)
def test_anything_else_leaves_scanning_off(monkeypatch, value):
    monkeypatch.setenv("SCAN_ENABLED", value)
    assert load_config().scan_enabled is False


def test_scan_gate_is_independent_of_mode(monkeypatch):
    """Turning scanning off must not depend on, or alter, the PAPER/LIVE guard."""
    for mode in ("PAPER", "LIVE"):
        monkeypatch.setenv("MODE", mode)
        config = load_config()
        assert config.mode == mode
        assert config.scan_enabled is False
