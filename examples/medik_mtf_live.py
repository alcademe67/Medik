"""MEDIK MTF LIVE — scan, evaluate, and place bracket orders through your own TWS.

Run this ON YOUR MACHINE with TWS open:

    python examples/medik_mtf_live.py AAPL MSFT NVDA
    python examples/medik_mtf_live.py --watchlist        # uses strategy.universe
    python examples/medik_mtf_live.py AAPL --dry-run     # evaluate, place nothing

WHAT IT DOES
    Weekly closed candle -> Daily closed candle -> 15-minute CLOSED candle
    -> medik_mtf.generate_signal -> medik_live limits -> sized bracket order
    (entry limit + stop-loss + take-profit) -> ONE TYPED CONFIRMATION -> TWS.

WHAT IT WILL NOT DO
    Submit anything without you typing the confirmation for that specific
    order. There is no --yes flag and no unattended mode. That is this
    repo's execution policy (CLAUDE.md) and it is also why ibkr/orders.py
    takes confirm=True. Do not add one.

    Emit SHORT signals unless the connected account can actually short.
    ALLOW_SHORT in config is an intent; --account-can-short is the
    assertion that IBKR permits it. In a TFSA a SELL does not open a short,
    it liquidates a holding, so the default is off.

EXPECTANCY WARNING — READ THIS
    Backtested over 201 symbols and 2 years of real IBKR daily bars, net of
    this account's commission schedule, the structure has a gross profit
    factor of 1.21 but is NET NEGATIVE below roughly $3,500 of equity: at
    $294 it returned -41.1%/yr, because commission is ~2% of a $29 position.
    Below MIN_VIABLE_EQUITY this script requires an extra typed
    acknowledgement. That is not a safety rail against a bug -- it is a
    tested result about this account size.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import Stock

from ibkr.client import IBKRClient
from strategy.medik_live import (
    DEFAULT_LIMITS,
    SessionState,
    check_new_trade,
    register_fill,
    resolve_allow_short,
)
from strategy.medik_mtf import (
    ATR_STOP_MULTIPLIER,
    RISK_REWARD,
    OHLCV,
    atr,
    drop_forming_bar,
    generate_signal,
    volume_ratio_of,
)

NY = ZoneInfo("America/New_York")
MIN_VIABLE_EQUITY = 3_500.0
RISK_PCT = 1.0
POSITION_PCT = 10.0

PER_SHARE, MIN_COMMISSION, MAX_PCT = 0.005, 1.00, 0.01


def commission(shares: float, value: float) -> float:
    if shares <= 0 or value <= 0:
        return 0.0
    return min(max(PER_SHARE * shares, MIN_COMMISSION), MAX_PCT * value)


def _to_bars(raw) -> list[OHLCV]:
    return [OHLCV(b.open, b.high, b.low, b.close, float(b.volume or 0)) for b in raw]


def _market_open(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    return (9, 30) <= (now.hour, now.minute) < (16, 0)


def fetch(ib, contract, duration: str, bar_size: str) -> list[OHLCV]:
    raw = ib.reqHistoricalData(
        contract, endDateTime="", durationStr=duration, barSizeSetting=bar_size,
        whatToShow="TRADES", useRTH=True, formatDate=1,
    )
    return _to_bars(raw)


def evaluate(ib, symbol: str, state: SessionState, can_short: bool):
    """Fetch all three timeframes and return (signal, contract, bars) or None."""
    qualified = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
    if not qualified:
        print(f"  {symbol}: could not qualify contract")
        return None
    contract = qualified[0]

    weekly = fetch(ib, contract, "2 Y", "1 week")
    daily = fetch(ib, contract, "1 Y", "1 day")
    entry = fetch(ib, contract, "10 D", "15 mins")

    # Closed candles only. reqHistoricalData returns the in-progress bar last
    # while the market is open; it is not a completed observation.
    open_now = _market_open(datetime.now(NY))
    weekly = drop_forming_bar(weekly, bar_complete=not open_now)
    daily = drop_forming_bar(daily, bar_complete=not open_now)
    entry = drop_forming_bar(entry, bar_complete=not open_now)

    if len(entry) < 40 or len(daily) < 60 or len(weekly) < 40:
        print(f"  {symbol}: insufficient history "
              f"(w={len(weekly)} d={len(daily)} e={len(entry)})")
        return None

    avg_daily_volume = sum(b.volume for b in daily[-60:]) / min(60, len(daily))
    sig = generate_signal(
        symbol=symbol, weekly=weekly, daily=daily, entry=entry,
        avg_daily_volume=avg_daily_volume,
        volume_ratio=volume_ratio_of(entry),
        account_can_short=can_short,
    )
    return sig, contract, entry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="*", help="symbols to evaluate")
    parser.add_argument("--watchlist", action="store_true", help="use strategy.universe")
    parser.add_argument("--dry-run", action="store_true", help="evaluate only, place nothing")
    parser.add_argument("--account-can-short", action="store_true",
                        help="assert the connected account is permitted to short")
    args = parser.parse_args()

    symbols = args.symbols
    if args.watchlist:
        from strategy.universe import UNIVERSE
        symbols = list(UNIVERSE)
    if not symbols:
        parser.error("give at least one symbol, or --watchlist")

    now = datetime.now(NY)
    print(f"MEDIK MTF LIVE — {now:%Y-%m-%d %H:%M} ET "
          f"({'market OPEN' if _market_open(now) else 'market CLOSED'})")
    if not _market_open(now) and not args.dry_run:
        print("Market is closed. A DAY bracket placed now cannot work, and a limit")
        print("priced off a stale quote is wrong against the next open. Use --dry-run")
        print("to evaluate, or re-run during 09:30-16:00 ET.")
        sys.exit(1)

    client = IBKRClient()
    ib = client.connect(retries=2)
    try:
        accounts = ib.managedAccounts()
        values = {r.tag: r.value for r in ib.accountValues() if r.currency in ("USD", "")}
        equity = float(values.get("NetLiquidation", 0) or 0)
        print(f"account {', '.join(accounts)}   equity ${equity:,.2f}")

        can_short, why = resolve_allow_short(DEFAULT_LIMITS, args.account_can_short)
        print(f"shorting: {'ENABLED' if can_short else 'suppressed'} — {why}")

        if equity < MIN_VIABLE_EQUITY and not args.dry_run:
            print()
            print("*" * 68)
            print(f"TESTED NEGATIVE EXPECTANCY AT THIS ACCOUNT SIZE")
            print(f"  equity ${equity:,.2f} is below the ${MIN_VIABLE_EQUITY:,.0f} break-even")
            print("  measured over 201 symbols / 2 years of real bars, net of commission.")
            print("  At $294 this structure returned -41.1%/yr: the edge is real")
            print("  (gross profit factor 1.21) but commission is ~2% per round trip.")
            print("*" * 68)
            if input('Type "I ACCEPT NEGATIVE EXPECTANCY" to continue: ').strip() \
                    != "I ACCEPT NEGATIVE EXPECTANCY":
                print("Stopped.")
                return

        state = SessionState(
            equity=equity,
            equity_start_of_day=float(values.get("PreviousDayEquityWithLoanValue", equity) or equity),
            equity_start_of_week=equity,   # refine from the journal if you track it
            open_symbols={p.contract.symbol: 1 for p in ib.positions() if p.position},
        )
        print(f"open positions: {sorted(state.open_symbols) or 'none'}\n")

        for symbol in symbols:
            found = evaluate(ib, symbol, state, can_short)
            if found is None:
                continue
            sig, contract, entry_bars = found

            if sig.action == "HOLD":
                print(f"  {symbol:<6} HOLD  — {sig.reason}")
                continue

            allowed, reason = check_new_trade(symbol, state, DEFAULT_LIMITS)
            if not allowed:
                print(f"  {symbol:<6} {sig.action} BLOCKED — {reason}")
                continue

            a = atr(entry_bars)[-1]
            entry_px = sig.price
            stop = entry_px - a * ATR_STOP_MULTIPLIER
            target = entry_px + (entry_px - stop) * RISK_REWARD
            risk_per_share = entry_px - stop
            if risk_per_share <= 0:
                print(f"  {symbol:<6} skipped — non-positive risk per share")
                continue

            qty = min(equity * RISK_PCT / 100.0 / risk_per_share,
                      equity * POSITION_PCT / 100.0 / entry_px)
            qty = round(qty, 4)
            value = qty * entry_px
            rt = commission(qty, value) * 2

            print(f"\n  {symbol}  {sig.action}  confidence {sig.confidence:.2f}")
            print(f"    weekly={sig.weekly_trend} daily={sig.daily_trend}")
            print(f"    entry  ${entry_px:,.2f}   qty {qty}   value ${value:,.2f}")
            print(f"    stop   ${stop:,.2f}  (-{risk_per_share / entry_px:.2%}, 1.5xATR)")
            print(f"    target ${target:,.2f}  (+{(target - entry_px) / entry_px:.2%}, 2R)")
            print(f"    risk   ${qty * risk_per_share:,.2f}   round-trip commission "
                  f"${rt:,.2f} ({rt / value:.2%} of position)")
            if qty < 1:
                print("    NOTE: fractional quantity — IBKR may reject bracket/OCA")
                print("          orders on fractional shares. Verify in the Orders tab.")

            if args.dry_run:
                print("    [dry run — not placed]")
                continue

            if input(f'    Type "BUY {symbol}" to place this bracket: ').strip() \
                    != f"{sig.action} {symbol}":
                print("    skipped")
                continue

            bracket = ib.bracketOrder(sig.action, qty, limitPrice=round(entry_px, 2),
                                      takeProfitPrice=round(target, 2),
                                      stopLossPrice=round(stop, 2))
            for order in bracket:
                order.tif = "DAY"
                ib.placeOrder(contract, order)
            ib.sleep(2)
            register_fill(state, symbol)
            print(f"    submitted — parent id {bracket[0].orderId}. "
                  "Verify all three legs in the TWS Orders tab.")
    finally:
        client.disconnect()
        print("\nDisconnected.")


if __name__ == "__main__":
    main()
