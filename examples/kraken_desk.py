"""Kraken desk -- a READ-ONLY report: market quotes, balances, open orders.

It places, modifies and cancels nothing. kraken.client refuses every
trading and funding endpoint, and the API key should not have those
permissions in the first place. To act on anything shown here, use the
Kraken app yourself.

Run ON YOUR MACHINE (api.kraken.com is blocked from the Claude cloud
container):

    python examples/kraken_desk.py                     # quotes only, no key needed
    python examples/kraken_desk.py XBTUSD ETHUSD ADAUSD
    python examples/kraken_desk.py --quote CAD         # value balances in CAD
    python examples/kraken_desk.py --save              # also write to reports/

Balances and open orders appear only when KRAKEN_API_KEY and
KRAKEN_API_SECRET are set in the environment. Create that key in Kraken
(Settings -> API) with ONLY "Query Funds" and "Query Open Orders & Trades"
checked. Never paste the secret into a chat or commit it.

Context, stated so the report isn't misread: this is a separate account
from the IBKR TFSA, and none of the strategies in this repo were tested on
crypto. The quotes here are information, not signals.
"""
from __future__ import annotations

import argparse
import io
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kraken.client import KrakenClient, KrakenError, MissingCredentials
from kraken.desk import DEFAULT_WATCHLIST, account_snapshot, open_orders, quotes
from paths import report_path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pairs", nargs="*", help=f"pairs to quote (default: {' '.join(DEFAULT_WATCHLIST)})")
    p.add_argument("--quote", default="USD", help="currency to value balances in (default: USD)")
    p.add_argument("--save", action="store_true", help="also write the report to reports/ (gitignored)")
    return p.parse_args()


def _clock() -> str:
    now = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        local = now.astimezone(ZoneInfo("America/Vancouver")).strftime("%Y-%m-%d %H:%M %Z")
        return f"{now:%Y-%m-%d %H:%M} UTC  /  {local}"
    except Exception:  # noqa: BLE001 - tzdata missing on some Windows Pythons
        return f"{now:%Y-%m-%d %H:%M} UTC"


def report(client: KrakenClient, args: argparse.Namespace) -> None:
    print(f"KRAKEN DESK (read-only)   {_clock()}")
    status = client.public("SystemStatus")
    print(f"Exchange status: {status.get('status', '?')}")
    print()

    pairs_meta = client.public("AssetPairs")
    wanted = [p.upper() for p in args.pairs] or list(DEFAULT_WATCHLIST)
    print(f"{'pair':<12}{'last':>14}{'bid':>14}{'ask':>14}{'spread':>9}{'since 00 UTC':>14}{'24h range':>26}")
    for q in quotes(client, wanted, pairs_meta):
        rng = f"{q.low_24h:,.4g} - {q.high_24h:,.4g}"
        print(
            f"{q.name:<12}{q.last:>14,.6g}{q.bid:>14,.6g}{q.ask:>14,.6g}"
            f"{q.spread_bps:>7.1f}bp{q.change_pct:>+13.2f}%{rng:>26}"
        )
    print()

    if not client.has_credentials:
        print("Balances / open orders: skipped (KRAKEN_API_KEY / KRAKEN_API_SECRET not set).")
        return

    quote_key, holdings = account_snapshot(client, args.quote.upper())
    print(f"BALANCES (valued in {args.quote.upper()} at mid)")
    total = 0.0
    for h in holdings:
        if h.value is None:
            print(f"  {h.asset:<10}{h.amount:>18,.8f}   (no direct {args.quote.upper()} pair; not valued)")
            continue
        total += h.value
        print(f"  {h.asset:<10}{h.amount:>18,.8f}  @ {h.price:>14,.6g}  = {h.value:>12,.2f}")
    if not holdings:
        print("  (no non-zero balances)")
    print(f"  {'total':<10}{'':>18}  {'':>16}  = {total:>12,.2f} {args.quote.upper()}")
    unpriced = [h.asset for h in holdings if h.value is None]
    if unpriced:
        print(f"  total excludes {', '.join(unpriced)}")
    print()

    orders = open_orders(client)
    print(f"OPEN ORDERS ({len(orders)})")
    for o in orders:
        print(f"  {o.opened_utc:%Y-%m-%d %H:%M}Z  {o.status:<8} {o.description}  (filled {o.filled:g}/{o.volume:g})")
    if orders:
        print("  To cancel or change any of these, use the Kraken app -- this desk cannot.")


def main() -> None:
    args = parse_args()
    client = KrakenClient.from_env()
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            report(client, args)
    except (KrakenError, MissingCredentials) as exc:
        sys.stdout.write(buf.getvalue())
        raise SystemExit(f"\nKraken desk stopped: {exc}")
    text = buf.getvalue()
    sys.stdout.write(text)
    if args.save:
        path = report_path("kraken-desk")
        path.write_text(text, encoding="utf-8")
        print(f"\nsaved {path}")


if __name__ == "__main__":
    main()
