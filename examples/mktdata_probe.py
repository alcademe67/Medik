"""READ-ONLY market-data diagnostic. Requests quotes for ONE symbol, places
nothing, and reports exactly which request shapes IBKR accepts.

    python examples\\mktdata_probe.py            # SPY
    python examples\\mktdata_probe.py TQQQ
    python examples\\mktdata_probe.py SPY --exchanges

WHY THIS EXISTS

The ETF bot gets error 10089 -- "the market data subscribed with the user does
not extend support for API use" -- for every US ETF, while TWS shows a live
"US Real-Time Non-Consolidated Streaming Quotes" subscription, fee waived.

Those two facts are compatible, because IBKR sells STREAMING and SNAPSHOT
quotes as separate entitlements. The bot's quote call is:

    ib.reqTickers(contract)

and ib_async implements that as

    self.client.reqMktData(reqId, contract, "", True, regulatorySnapshot, [])
                                                ^^^^ snapshot=True

so the bot has been asking for snapshots throughout, which the streaming
subscription does not cover. This script tests that hypothesis directly by
running the SAME symbol through both shapes and printing which one IBKR
answers.

It also sweeps the four market-data types, because the bot never calls
reqMarketDataType() at all -- so it runs at type 1 (live) and errors rather
than degrading to delayed data.

SAFETY
  * connects with readonly=True, so TWS rejects any order from this process;
    the guarantee is the broker's, not this file being careful
  * imports nothing from the live runner's trading path
  * cancels every streaming subscription it opens
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import IB, Stock

from ibkr.accounts import resolve_account

MD_TYPES = {
    1: "live",
    2: "frozen (last live values, market closed)",
    3: "delayed (~15 min)",
    4: "delayed-frozen",
}

# Error codes that mean "you may not have this data", as opposed to noise.
ENTITLEMENT_CODES = {10089, 10090, 10091, 10167, 10168, 10197, 354, 162}
# Codes IBKR sends as status, not failure.
BENIGN_CODES = {2104, 2106, 2107, 2108, 2119, 2158, 2100, 2103, 2105, 2157}


def ok(x) -> bool:
    """IBKR uses NaN and -1 for 'no value'. NaN is truthy, so test explicitly."""
    return x is not None and not (isinstance(x, float) and math.isnan(x)) and x > 0


def fmt(x) -> str:
    if x is None:
        return "None"
    if isinstance(x, float) and math.isnan(x):
        return "NaN"
    if isinstance(x, float) and x == -1:
        return "-1 (no value)"
    return f"{x:,.4f}" if isinstance(x, float) else str(x)


class ErrorLog:
    """Collects IBKR errors so each probe can report its own."""

    def __init__(self, ib: IB):
        self.entries: list[tuple] = []
        ib.errorEvent += self._on_error

    def _on_error(self, reqId, code, msg, contract):
        self.entries.append((reqId, code, (msg or "").strip()))

    def take(self) -> list[tuple]:
        out, self.entries = self.entries, []
        return out


def report_errors(errors) -> bool:
    """Print errors, return True if any looked like an entitlement problem."""
    entitlement = False
    for reqId, code, msg in errors:
        if code in BENIGN_CODES:
            continue
        flag = "  <-- ENTITLEMENT" if code in ENTITLEMENT_CODES else ""
        entitlement = entitlement or code in ENTITLEMENT_CODES
        print(f"      ERROR {code} (reqId {reqId}): {msg}{flag}")
    return entitlement


def show(ticker) -> bool:
    """Print a ticker's prices. Returns True if anything usable came back."""
    fields = [("bid", ticker.bid), ("ask", ticker.ask), ("last", ticker.last),
              ("close", ticker.close)]
    print("      " + "  ".join(f"{name}={fmt(val)}" for name, val in fields))
    usable = any(ok(v) for _, v in fields)
    marker = getattr(ticker, "marketDataType", None)
    if marker:
        print(f"      marketDataType reported by IBKR: {marker} "
              f"({MD_TYPES.get(marker, '?')})")
    return usable


def probe_streaming(ib, log, contract, wait: float) -> bool:
    """reqMktData(snapshot=False) -- what the free streaming feed covers."""
    print("    STREAMING  reqMktData(genericTickList='', snapshot=False)")
    ticker = ib.reqMktData(contract, "", False, False)
    ib.sleep(wait)
    usable = show(ticker)
    entitlement = report_errors(log.take())
    ib.cancelMktData(contract)
    ib.sleep(0.2)
    return usable and not entitlement


def probe_snapshot(ib, log, contract, wait: float) -> bool:
    """reqTickers() -- snapshot=True. This is what the ETF bot calls today."""
    print("    SNAPSHOT   reqTickers()  [snapshot=True — the bot's current call]")
    try:
        tickers = ib.reqTickers(contract)
    except Exception as exc:
        print(f"      raised {type(exc).__name__}: {exc}")
        report_errors(log.take())
        return False
    usable = show(tickers[0]) if tickers else False
    if not tickers:
        print("      no ticker returned")
    entitlement = report_errors(log.take())
    return usable and not entitlement


def probe_historical(ib, log, contract, wait: float) -> bool:
    """The bot's other data call, entitled separately from quotes."""
    print("    HISTORICAL reqHistoricalData(5 mins, TRADES, useRTH=True)")
    try:
        bars = ib.reqHistoricalData(
            contract, endDateTime="", durationStr="1 D",
            barSizeSetting="5 mins", whatToShow="TRADES", useRTH=True,
            formatDate=1)
    except Exception as exc:
        print(f"      raised {type(exc).__name__}: {exc}")
        report_errors(log.take())
        return False
    print(f"      {len(bars)} bars"
          + (f", last close {bars[-1].close:,.2f} at {bars[-1].date}" if bars else ""))
    entitlement = report_errors(log.take())
    return bool(bars) and not entitlement


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbol", nargs="?", default="SPY")
    ap.add_argument("--wait", type=float, default=4.0,
                    help="seconds to wait for streaming ticks (default 4)")
    ap.add_argument("--exchanges", action="store_true",
                    help="also try routing to specific exchanges, not just SMART")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7496)
    ap.add_argument("--client-id", type=int, default=78)
    args = ap.parse_args()
    symbol = args.symbol.upper()

    ib = IB()
    print(f"connecting {args.host}:{args.port} clientId={args.client_id} readonly=True")
    ib.connect(args.host, args.port, clientId=args.client_id, timeout=15,
               readonly=True)
    log = ErrorLog(ib)

    try:
        managed = list(ib.managedAccounts())
        account, why = resolve_account(managed, env_vars=("MEDIK_ETF_ACCOUNT",
                                                          "IBKR_ACCOUNT"))
        print(f"managed accounts: {managed}")
        print(f"account: {why}")
        print(f"server version: {ib.client.serverVersion()}")
        log.take()          # drop connection-time status messages

        print(f"\nqualifying {symbol} on SMART/USD ...")
        qualified = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
        if not qualified:
            print(f"could not qualify {symbol}")
            report_errors(log.take())
            return 2
        contract = qualified[0]
        print(f"  conId={contract.conId}  symbol={contract.symbol}  "
              f"exchange={contract.exchange}  primaryExchange={contract.primaryExchange}  "
              f"currency={contract.currency}  secType={contract.secType}")
        report_errors(log.take())

        results = {}
        for md_type in (1, 2, 3, 4):
            print(f"\n{'=' * 70}")
            print(f"MARKET DATA TYPE {md_type} — {MD_TYPES[md_type]}")
            print("=" * 70)
            ib.reqMarketDataType(md_type)
            ib.sleep(0.5)
            log.take()
            results[(md_type, "streaming")] = probe_streaming(ib, log, contract, args.wait)
            results[(md_type, "snapshot")] = probe_snapshot(ib, log, contract, args.wait)
            results[(md_type, "historical")] = probe_historical(ib, log, contract, args.wait)

        if args.exchanges:
            # Only meaningful if SMART failed: a non-consolidated feed carries
            # specific venues, so naming one can succeed where SMART does not.
            print(f"\n{'=' * 70}")
            print("DIRECT EXCHANGE ROUTING (market data type 1)")
            print("=" * 70)
            ib.reqMarketDataType(1)
            for exch in ("ARCA", "ISLAND", "IEX", "BATS", "NYSE"):
                print(f"  --- {exch} ---")
                try:
                    q = ib.qualifyContracts(Stock(symbol, exch, "USD"))
                except Exception as exc:
                    print(f"      qualify raised {type(exc).__name__}: {exc}")
                    log.take()
                    continue
                if not q:
                    print("      could not qualify")
                    report_errors(log.take())
                    continue
                log.take()
                results[(f"{exch}", "streaming")] = probe_streaming(ib, log, q[0], args.wait)

        print(f"\n{'=' * 70}")
        print("SUMMARY")
        print("=" * 70)
        for (key, mode), good in results.items():
            label = f"type {key}" if isinstance(key, int) else f"{key} (type 1)"
            print(f"  {label:<28} {mode:<12} {'OK' if good else 'no data'}")

        live_stream = results.get((1, "streaming"))
        live_snap = results.get((1, "snapshot"))
        print()
        if live_stream and not live_snap:
            print("VERDICT: streaming works at type 1, snapshot does not.")
            print("  The free 'US Real-Time Non-Consolidated Streaming Quotes'")
            print("  subscription covers STREAMING quotes only. reqTickers() asks")
            print("  for a snapshot, which is a separate IBKR entitlement.")
            print("  FIX: replace reqTickers() with reqMktData(snapshot=False)")
            print("  in build_snapshot(). No purchase needed.")
        elif live_stream and live_snap:
            print("VERDICT: both work at type 1. Error 10089 is NOT coming from")
            print("  the quote call — re-check reqHistoricalData above, and which")
            print("  symbols actually failed.")
        elif not live_stream and results.get((3, "streaming")):
            print("VERDICT: no real-time US equity subscription on this account.")
            print("  Type 1 fails for BOTH streaming and snapshot; type 3")
            print("  (delayed) works; historical works. Historical data is")
            print("  entitled separately, which is why it succeeds.")
            print()
            print("  There is NO 'enable API access' setting. A market-data")
            print("  subscription entitles TWS and the API identically -- if")
            print("  TWS shows real-time prices, the API gets them too. So this")
            print("  is not a configuration problem to solve in code or in TWS.")
            print()
            print("  The subscription has to be ADDED in Client Portal:")
            print("    Settings > Account Settings > Market Data Subscriptions")
            print("  Check the CURRENT subscriptions list, not the available-to-add")
            print("  list -- 'Fee Waived' next to an offer is a price, not proof")
            print("  that it is active.")
            print()
            print("  Also check Market Data Subscriber Status: the free and")
            print("  low-cost US feeds are NON-PROFESSIONAL only.")
        else:
            print("VERDICT: no quote path returned data. Check that the market is")
            print("  open, then read the per-request errors above.")

        print("\nno orders were placed; the connection was read-only")
        return 0
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
