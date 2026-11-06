"""Parsing and valuation for the Kraken desk. Read-only; see kraken/__init__.py.

Kraken quirks handled here, so callers don't rediscover them:

- **Pair names.** You ask for "XBTUSD", Kraken answers under "XXBTZUSD".
  Results are keyed by Kraken's name; `display_name` maps back to "XBT/USD".
- **The last OHLC row is still forming.** Kraken returns the current,
  uncommitted bar as the final entry. Same lesson as rule 7 in CLAUDE.md
  (a mid-session IBKR bar caused a false gate failure on 2026-08-03): it is
  dropped unless its interval has fully elapsed.
- **720-row cap.** OHLC returns at most the 720 most recent bars whatever
  `since` says, so daily history tops out at ~2 years. That is a Kraken API
  limit, not a bug here.
- **Asset codes.** Balances use codes like XXBT, ZUSD, and suffixed variants
  for staked/earn balances (XBT.M, ETH.F, DOT.S). Valuation strips the
  suffix and matches on the Assets altname.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import pandas as pd

from .client import KrakenClient

DAILY = 1440  # minutes
DEFAULT_WATCHLIST = ("XBTUSD", "ETHUSD", "SOLUSD")


def _pairs_only(result: dict) -> dict:
    """Drop OHLC's "last" cursor so only pair-keyed entries remain."""
    return {k: v for k, v in result.items() if k != "last"}


# --------------------------------------------------------------------- bars

def ohlc_frame(rows: list, interval_minutes: int = DAILY, now: float | None = None) -> pd.DataFrame:
    """Kraken OHLC rows -> frame in the ibkr.cache column layout.

    Row format: [time, open, high, low, close, vwap, volume, count], values
    as strings. A row whose interval has not fully elapsed by `now` is
    dropped -- see the module docstring.
    """
    now = time.time() if now is None else now
    span = interval_minutes * 60
    complete = [r for r in rows if int(r[0]) + span <= now]
    frame = pd.DataFrame(
        {
            "open": [float(r[1]) for r in complete],
            "high": [float(r[2]) for r in complete],
            "low": [float(r[3]) for r in complete],
            "close": [float(r[4]) for r in complete],
            "volume": [float(r[6]) for r in complete],
        },
        index=pd.to_datetime([int(r[0]) for r in complete], unit="s"),
    )
    frame.index.name = "time"
    return frame.sort_index()


def daily_bars(client: KrakenClient, pair: str, now: float | None = None) -> tuple[str, pd.DataFrame]:
    """Completed daily bars for one pair. Returns (kraken_pair_key, frame)."""
    result = _pairs_only(client.public("OHLC", pair=pair, interval=DAILY))
    if len(result) != 1:
        raise ValueError(f"{pair}: expected one pair in OHLC result, got {sorted(result)}")
    key, rows = next(iter(result.items()))
    return key, ohlc_frame(rows, DAILY, now)


# ------------------------------------------------------------------- ticker

@dataclass(frozen=True)
class Quote:
    pair: str          # Kraken's key, e.g. XXBTZUSD
    name: str          # display, e.g. XBT/USD
    bid: float
    ask: float
    last: float
    open_utc: float    # today's open, 00:00 UTC
    high_24h: float
    low_24h: float
    volume_24h: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid and self.ask else self.last

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 1e4 if self.mid else float("nan")

    @property
    def change_pct(self) -> float:
        """Change since 00:00 UTC (= 16:00/17:00 Pacific the previous day)."""
        return (self.last / self.open_utc - 1) * 100 if self.open_utc else float("nan")


def display_name(pair_key: str, pairs: dict | None) -> str:
    info = (pairs or {}).get(pair_key) or {}
    return info.get("wsname") or info.get("altname") or pair_key


def parse_ticker(result: dict, pairs: dict | None = None) -> list[Quote]:
    quotes = []
    for key, t in result.items():
        quotes.append(
            Quote(
                pair=key,
                name=display_name(key, pairs),
                bid=float(t["b"][0]),
                ask=float(t["a"][0]),
                last=float(t["c"][0]),
                open_utc=float(t["o"]),
                high_24h=float(t["h"][1]),
                low_24h=float(t["l"][1]),
                volume_24h=float(t["v"][1]),
            )
        )
    return quotes


def quotes(client: KrakenClient, pairs_wanted, pairs: dict | None = None) -> list[Quote]:
    return parse_ticker(client.public("Ticker", pair=",".join(pairs_wanted)), pairs)


# ------------------------------------------------------------------ account

def _base_candidates(asset: str, altnames: dict[str, str]) -> set[str]:
    stripped = asset.split(".")[0]
    cands = {stripped}
    cands |= {key for key, alt in altnames.items() if alt == stripped}
    if stripped in altnames:
        cands.add(altnames[stripped])
    return cands


def resolve_quote_asset(quote: str, altnames: dict[str, str]) -> str:
    """"USD" -> "ZUSD" (Kraken's asset key), via the Assets altnames."""
    for key, alt in altnames.items():
        if alt == quote or key == quote:
            return key
    return quote


def pricing_pairs(balances: dict, pairs: dict, altnames: dict[str, str], quote_key: str) -> dict:
    """asset -> Kraken pair key to price it in `quote_key`, "QUOTE" if it IS
    the quote currency, or None if Kraken lists no direct pair."""
    quote_alt = altnames.get(quote_key, quote_key)
    out: dict[str, str | None] = {}
    for asset in balances:
        cands = _base_candidates(asset, altnames)
        if cands & {quote_key, quote_alt}:
            out[asset] = "QUOTE"
            continue
        out[asset] = next(
            (
                name
                for name, info in sorted(pairs.items())
                if not name.endswith(".d")  # dark-pool duplicates
                and info.get("base") in cands
                and info.get("quote") == quote_key
            ),
            None,
        )
    return out


@dataclass(frozen=True)
class Holding:
    asset: str
    amount: float
    price: float | None   # in the quote currency; None = unpriced
    pair: str | None

    @property
    def value(self) -> float | None:
        return None if self.price is None else self.amount * self.price


def value_balances(balances: dict, routes: dict, quotes_by_pair: dict[str, Quote]) -> list[Holding]:
    holdings = []
    for asset, raw in balances.items():
        amount = float(raw)
        if amount == 0:
            continue
        route = routes.get(asset)
        if route == "QUOTE":
            price, pair = 1.0, None
        elif route and route in quotes_by_pair:
            price, pair = quotes_by_pair[route].mid, route
        else:
            price, pair = None, route
        holdings.append(Holding(asset, amount, price, pair))
    holdings.sort(key=lambda h: -(h.value or 0))
    return holdings


def account_snapshot(client: KrakenClient, quote: str = "USD") -> tuple[str, list[Holding]]:
    """Balances valued in `quote`. Returns (quote_asset_key, holdings)."""
    balances = client.private("Balance")
    altnames = {k: v.get("altname", k) for k, v in client.public("Assets").items()}
    pairs = client.public("AssetPairs")
    quote_key = resolve_quote_asset(quote, altnames)
    routes = pricing_pairs(balances, pairs, altnames, quote_key)
    needed = sorted({r for r in routes.values() if r and r != "QUOTE"})
    by_pair = {q.pair: q for q in quotes(client, needed, pairs)} if needed else {}
    return quote_key, value_balances(balances, routes, by_pair)


@dataclass(frozen=True)
class OpenOrder:
    txid: str
    description: str   # e.g. "buy 0.01 XBTUSD @ limit 60000"
    status: str
    volume: float
    filled: float
    opened_utc: pd.Timestamp


def parse_open_orders(result: dict) -> list[OpenOrder]:
    out = []
    for txid, o in (result.get("open") or {}).items():
        out.append(
            OpenOrder(
                txid=txid,
                description=(o.get("descr") or {}).get("order", "?"),
                status=o.get("status", "?"),
                volume=float(o.get("vol", 0)),
                filled=float(o.get("vol_exec", 0)),
                opened_utc=pd.Timestamp(float(o.get("opentm", 0)), unit="s", tz="UTC"),
            )
        )
    return sorted(out, key=lambda o: o.opened_utc)


def open_orders(client: KrakenClient) -> list[OpenOrder]:
    return parse_open_orders(client.private("OpenOrders"))
