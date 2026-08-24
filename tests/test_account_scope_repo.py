"""No script may read the broker across the whole login again.

Every ib_async read spans all accounts under the login. This file is the
regression net: a new script that reduces account rows to {tag: value}, or
iterates positions() unfiltered, fails here rather than in production with a
funded account reading $0.00 or an order placed in the wrong account.

Checks run against the source with comments and string literals blanked out,
so prose describing the old pattern -- including this docstring -- cannot
trigger or mask a finding.
"""
from __future__ import annotations

import io
import pathlib
import re
import tokenize

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SOURCES = sorted(
    p for p in list((REPO / "examples").glob("*.py"))
    + list((REPO / "service").glob("*.py"))
    + list((REPO / "ibkr").glob("*.py"))
    if p.name != "accounts.py"          # where the scoping is defined
)

# Tokens that show an account filter is being applied nearby.
SCOPED = ("belongs_to", "positions_for", "tag_map", "account_context",
          "order_belongs_to", "mine(", "account)", "account,")

WINDOW = 240      # characters either side of a match to search for SCOPED


def code_only(src: str) -> str:
    """Source with comments and string literals blanked, offsets preserved."""
    buf = list(src)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError):
        return src
    lines = src.splitlines(keepends=True)
    starts, total = [], 0
    for line in lines:
        starts.append(total)
        total += len(line)
    for tok in tokens:
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (r1, c1), (r2, c2) = tok.start, tok.end
        begin, end = starts[r1 - 1] + c1, starts[r2 - 1] + c2
        for i in range(begin, min(end, len(buf))):
            if buf[i] != "\n":
                buf[i] = " "
    return "".join(buf)


def _scoped_near(code: str, at: int) -> bool:
    return any(t in code[max(0, at - WINDOW):at + WINDOW] for t in SCOPED)


def _ids(paths):
    return [str(p.relative_to(REPO)) for p in paths]


COLLAPSE = re.compile(r"\{\s*(?:row|r)\.tag:\s*(?:row|r)\.value\s+for")
ROW_SCAN = re.compile(r"for\s+row\s+in\s+ib\.account(?:Summary|Values)\(\s*\)")
POSITIONS = re.compile(r"\bib\.positions\(\s*\)")
PLACE_ORDER = re.compile(r"\bib\.placeOrder\(")


@pytest.mark.parametrize("path", SOURCES, ids=_ids(SOURCES))
def test_account_rows_are_not_collapsed_by_tag(path):
    code = code_only(path.read_text())
    for m in COLLAPSE.finditer(code):
        assert _scoped_near(code, m.start()), (
            f"{path.name} builds {{tag: value}} without filtering by account; "
            "use ibkr.accounts.tag_map")
    for m in ROW_SCAN.finditer(code):
        assert _scoped_near(code, m.start()), (
            f"{path.name} scans accountSummary()/accountValues() unfiltered; "
            "use ibkr.accounts.tag_map or account_context")


@pytest.mark.parametrize("path", SOURCES, ids=_ids(SOURCES))
def test_positions_are_filtered_by_account(path):
    code = code_only(path.read_text())
    for m in POSITIONS.finditer(code):
        line = code[:m.start()].count("\n") + 1
        assert _scoped_near(code, m.start()), (
            f"{path.name}:{line} iterates ib.positions() across every account")


@pytest.mark.parametrize("path", SOURCES, ids=_ids(SOURCES))
def test_placed_orders_name_their_account(path):
    """An untagged order on a multi-account login can be routed to the wrong
    account or rejected, so every submitting script must set order.account."""
    code = code_only(path.read_text())
    if not PLACE_ORDER.search(code):
        return
    assert re.search(r"\.account = account\b", code), (
        f"{path.name} calls ib.placeOrder but never sets .account on an order")


def test_the_helpers_are_actually_used_somewhere():
    """Guards against every check passing because nothing imports the module."""
    users = [p.name for p in SOURCES if "ibkr.accounts" in p.read_text()]
    assert len(users) >= 10, users


def test_the_detector_still_catches_the_original_bug(tmp_path):
    """The net must fail on the code that caused this, or it proves nothing."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def read(ib):\n"
        "    values = {r.tag: r.value for r in ib.accountValues()}\n"
        "    return values\n")
    code = code_only(bad.read_text())
    m = COLLAPSE.search(code)
    assert m and not _scoped_near(code, m.start())


def test_the_detector_is_not_fooled_by_prose(tmp_path):
    good = tmp_path / "good.py"
    good.write_text(
        'def read(ib):\n'
        '    """Do not write {r.tag: r.value for r in ib.accountValues()}."""\n'
        '    return None\n')
    assert not COLLAPSE.search(code_only(good.read_text()))
