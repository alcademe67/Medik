"""Expanded scan universe — ~200 liquid, actively-traded US names.

This is the static fallback universe used by the daily scan when the dynamic
IBKR market scanner (ibkr/scanner.py, needs a live TWS connection) isn't
available — e.g. when the scan runs from the cloud connector instead of the
owner's desktop TWS.

Selection bias: high average dollar-volume and/or high realized volatility,
across sectors, so the 4-indicator gate has a broad, tradeable pool to work
with. Sub-$3 perpetual pennies and illiquid names are deliberately excluded;
the gate + risk sizing filter the rest. Long-only accounts ignore SELL hits.
"""
from __future__ import annotations

LIQUID_UNIVERSE: tuple[str, ...] = (
    # Mega-cap tech / comms
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AVGO", "TSLA", "ORCL",
    "ADBE", "CRM", "NFLX", "IBM", "NOW", "INTU", "CSCO", "TXN", "QCOM",
    "AMD", "INTC", "MU", "AMAT", "LRCX", "KLAC", "MRVL", "ARM", "SMCI",
    "ON", "MCHP", "NXPI", "TSM", "ASML", "WOLF", "SNDK", "ANET", "DELL",
    "HPQ", "WDC", "STX", "CIEN",
    # Software / cloud / cyber / data
    "SNOW", "NET", "CRWD", "PANW", "ZS", "DDOG", "MDB", "PLTR", "SHOP",
    "TEAM", "WDAY", "HUBS", "DOCU", "OKTA", "TWLO", "U", "PATH", "S",
    "GTLB", "CFLT", "DT", "ESTC", "FTNT", "AI", "SOUN", "BBAI",
    # Internet / consumer discretionary online
    "DIS", "UBER", "LYFT", "ABNB", "BKNG", "DASH", "ETSY", "EBAY", "PINS",
    "SNAP", "RBLX", "SPOT", "ROKU", "DKNG", "CHWY", "W",
    # Fintech / payments / crypto-adjacent
    "V", "MA", "PYPL", "COIN", "HOOD", "SOFI", "AFRM", "UPST", "NU", "BILL",
    "MSTR", "MARA", "RIOT", "CLSK", "HUT", "BITF",
    # Big financials
    "JPM", "BAC", "WFC", "C", "GS", "MS", "SCHW", "BLK", "AXP", "KKR",
    # EV / auto
    "RIVN", "LCID", "NIO", "F", "GM",
    # Energy
    "XOM", "CVX", "COP", "OXY", "SLB", "HAL", "DVN", "MPC", "PSX", "VLO",
    "FANG", "EOG",
    # Health / biotech
    "LLY", "UNH", "JNJ", "PFE", "MRK", "ABBV", "TMO", "ISRG", "VRTX",
    "REGN", "MRNA", "HIMS", "TEM", "DXCM", "AMGN",
    # Industrials
    "BA", "CAT", "DE", "GE", "HON", "LMT", "RTX", "UPS", "UNP", "VRT",
    "ETN", "PWR", "GEV",
    # Consumer staples / retail / restaurants
    "WMT", "COST", "HD", "LOW", "NKE", "SBUX", "MCD", "TGT", "LULU", "ELF",
    "ANF", "CMG", "DECK", "CROX", "CAVA", "CELH", "MNST", "KO", "PEP",
    # High-beta momentum / retail favorites / quantum / space / nuclear
    "GME", "CVNA", "PLUG", "IONQ", "RGTI", "QBTS", "ASTS", "RKLB", "LUNR",
    "ACHR", "JOBY", "OKLO", "SMR", "NNE", "RKT", "DNA",
    # Media / telecom
    "T", "VZ", "TMUS", "CMCSA",
    # China ADRs
    "BABA", "PDD", "JD", "BIDU",
    # Materials / metals
    "FCX", "NEM", "ALB",
    # Airlines / travel
    "AAL", "DAL", "UAL", "CCL", "NCLH",
)

# de-dup guard (keeps the list honest if edited by hand)
assert len(LIQUID_UNIVERSE) == len(set(LIQUID_UNIVERSE)), "duplicate ticker in universe"
