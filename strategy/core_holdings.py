"""Approved core ETF holdings — the whitelist behind the ETF exemption from
the 20%-per-trade concentration cap (owner decision, 2026-08-05).

WHY A WHITELIST AND NOT "any ETF"
---------------------------------
The owner's instruction was "exempt ETFs from the 20% cap". Taken literally
that is unsafe, because "ETF" is a legal wrapper, not a risk profile. A
search for "QQQ" on IBKR returns, among others:

    QQQ    Invesco QQQ Trust          — 100 companies, unleveraged
    TQQQ   ProShares UltraPro QQQ     — +3x DAILY leveraged
    SQQQ   ProShares UltraPro Short   — -3x DAILY inverse
    QLD    ProShares Ultra QQQ        — +2x daily
    QID    ProShares UltraShort QQQ   — -2x daily

All five are ETFs. Committing 70% of the account to TQQQ is not a milder
version of committing 70% to QQQ — leveraged/inverse funds reset daily, so
volatility decay makes them structurally unsuitable for buy-and-hold and
they can lose most of their value in a sideways-but-choppy market without
the index going anywhere.

The 20% cap exists to limit *single-issuer* blowup risk. Relaxing it for a
fund holding 100+ companies is coherent; relaxing it for a 3x daily-reset
derivative product inverts the cap's purpose. So the exemption is granted
by explicit symbol, not by asset class.

Adding a symbol here is a deliberate act. The bar is: broadly diversified
(100+ holdings), unleveraged, non-inverse, index-tracking, and intended to
be held for years.
"""
from __future__ import annotations

# Broad-market, unleveraged, index-tracking funds approved for the raised cap.
CORE_ETFS: frozenset[str] = frozenset({
    # US large-cap / total market
    "SPY", "VOO", "IVV", "SPLG",      # S&P 500
    "QQQ", "QQQM",                     # Nasdaq-100
    "VTI", "ITOT", "SCHB",             # total US market
    "DIA",                             # Dow 30
    # US small/mid
    "IWM", "VB", "IJR", "IJH",
    # International / global
    "VT", "VXUS", "VEA", "IEFA", "VWO", "IEMG",
    # Investment-grade bonds
    "AGG", "BND",
})

# Not exhaustive — it cannot be, new leveraged products launch constantly.
# This exists so the *error message* can name the reason when someone tries
# a well-known leveraged ticker. The real protection is that CORE_ETFS is a
# whitelist: anything not on it is rejected regardless of this set.
KNOWN_LEVERAGED_OR_INVERSE: frozenset[str] = frozenset({
    "TQQQ", "SQQQ", "QLD", "QID", "PSQ", "QQQU", "QQQY", "QQXL", "QQUP",
    "UPRO", "SPXU", "SSO", "SDS", "SH", "SPXL", "SPXS",
    "UDOW", "SDOW", "TNA", "TZA", "UVXY", "SVXY", "VIXY",
    "SOXL", "SOXS", "TECL", "TECS", "FAS", "FAZ", "LABU", "LABD",
})


class NotACoreETF(Exception):
    """Raised when a symbol is not approved for the raised ETF position cap."""


def is_core_etf(symbol: str) -> bool:
    """True if symbol is approved for the raised (ETF) position cap."""
    return symbol.strip().upper() in CORE_ETFS


def assert_core_etf(symbol: str) -> str:
    """Return the normalized symbol, or raise NotACoreETF explaining why the
    raised cap does not apply to it."""
    sym = symbol.strip().upper()
    if sym in CORE_ETFS:
        return sym
    if sym in KNOWN_LEVERAGED_OR_INVERSE:
        raise NotACoreETF(
            f"{sym} is a leveraged or inverse product. These reset daily and "
            f"decay in choppy markets, so they are never eligible for the "
            f"raised ETF cap regardless of the underlying index."
        )
    raise NotACoreETF(
        f"{sym} is not an approved core ETF. The raised position cap applies "
        f"only to broadly-diversified unleveraged index funds "
        f"(see strategy/core_holdings.CORE_ETFS); everything else stays under "
        f"the standard per-trade concentration cap."
    )
