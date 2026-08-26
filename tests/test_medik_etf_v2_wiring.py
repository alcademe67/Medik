"""The live ETF bot runs the v2 configuration (owner decision, 2026-08-26).

Pins four facts about examples/medik_etf_live.py so a refactor cannot
silently revert them:

  1. the scan universe is exactly V2_UNIVERSE — no inverse funds;
  2. startup reconciliation still recognises every symbol either version
     ever traded, so a legacy inverse position is adopted, not orphaned;
  3. the entry path applies BOTH v2 gates (qualifies_v2 and
     net_edge_check) before authorize_order;
  4. the anti-repeat ledger uses v2's longer re-entry cooldown.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from strategy.medik_etf import ETF_UNIVERSE
from strategy.medik_etf_v2 import REENTRY_COOLDOWN_SEC_V2, V2_UNIVERSE

LIVE = REPO / "examples" / "medik_etf_live.py"

INVERSE = {"SQQQ", "SOXS", "TZA", "LABD", "FAZ", "ERY"}


def _load_live_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("medik_etf_live", LIVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scan_universe_is_v2_and_non_inverse():
    live = _load_live_module()
    assert live.SCAN_UNIVERSE == list(V2_UNIVERSE)
    assert not (set(live.SCAN_UNIVERSE) & INVERSE), \
        "inverse funds must not be scanned — v2 removed them for cost reasons"


def test_reconcile_universe_covers_both_versions():
    live = _load_live_module()
    got = set(live.RECONCILE_UNIVERSE)
    assert set(V2_UNIVERSE) <= got
    assert set(ETF_UNIVERSE) <= got, \
        "a legacy position in a v1-only symbol must still be recognised"


def test_scan_loop_uses_scan_universe_not_v1_list():
    src = LIVE.read_text(encoding="utf-8")
    assert "for symbol in SCAN_UNIVERSE:" in src
    assert "for symbol in ETF_UNIVERSE:" not in src


def test_entry_path_applies_both_v2_gates_before_authorization():
    src = LIVE.read_text(encoding="utf-8")
    i_qual = src.find("qualifies_v2(best)")
    i_edge = src.find("net_edge_check(sized.symbol")
    i_auth = src.find("auth = authorize_order(")
    assert 0 < i_qual < i_edge < i_auth, \
        "both v2 gates must run before authorize_order in scan_once"


def test_ledger_uses_v2_cooldown():
    src = LIVE.read_text(encoding="utf-8")
    assert re.search(r"TradeLedger\(cooldown_sec=REENTRY_COOLDOWN_SEC_V2\)", src)
    assert REENTRY_COOLDOWN_SEC_V2 == 1800
