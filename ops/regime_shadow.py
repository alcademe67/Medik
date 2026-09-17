"""Daily PAPER/SHADOW tracker for the frozen daily-regime strategy.

Runs once per trading day ALONGSIDE the live v2 bot and the live account, and
records what the daily-regime strategy WOULD do — so a real out-of-sample track
record accumulates before anyone decides whether to make it live. It shares the
exact decision logic (strategy.daily_regime) that the 20-year walk-forward
validated.

SAFETY — this is the whole point:
  * It NEVER places, stages, or cancels an order. There is no order path in this
    file at all; it only reqHistoricalData (read-only) and writes local files.
  * It touches nothing the live bot uses: its own state file, its own journal.
  * Stops cleanly on STOP_MEDIK, same kill switch as everything else.

Paper model: decision on the latest COMPLETED daily close, executed in paper at
that close +/- slippage with the account's real commission (a ~1-day-open
approximation, noted so the record isn't over-read). Each of the six ETFs is an
independent equal-weight long-or-cash sleeve; a cash sleeve earns 0. A
buy-and-hold sleeve is tracked beside each as the benchmark.

    python ops/regime_shadow.py            # process today's completed bar
    python ops/regime_shadow.py --status   # print the current paper standing
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from strategy.daily_regime import (  # noqa: E402
    ENTER, EXIT, inputs_from_closes, decide,
)

SYMBOLS = ["SPY", "QQQ", "IWM", "DIA", "XLF", "XLK"]
EQUITY_PER_SLEEVE = 10_000.0
PER_SHARE, MIN_COMM, MAX_PCT = 0.005, 1.00, 0.01
SLIPPAGE_BPS, HALF_SPREAD_BPS = 2.0, 1.0

LOG_DIR = REPO / "logs"
STATE_FILE = LOG_DIR / "regime_shadow_state.json"
JOURNAL = LOG_DIR / "regime_shadow_journal.jsonl"
STOP_FILE = REPO / "STOP_MEDIK"


def commission(sh, val):
    return 0.0 if sh <= 0 or val <= 0 else min(max(PER_SHARE * sh, MIN_COMM), MAX_PCT * val)


def _log(msg):
    LOG_DIR.mkdir(exist_ok=True)
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        (LOG_DIR / "regime_shadow.log").open("a", encoding="utf-8").write(line + "\n")
    except OSError:
        pass


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def save_state(st):
    LOG_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=2))


def fetch_daily():
    """Read-only 1Y daily bars for all six ETFs via TWS. Returns {sym: (dates, closes)}."""
    from ib_async import IB, Stock
    ib = IB()
    ib.connect("127.0.0.1", 7496, clientId=487, timeout=25, readonly=True)
    out = {}
    try:
        for s in SYMBOLS:
            c = Stock(s, "SMART", "USD"); ib.qualifyContracts(c)
            bars = ib.reqHistoricalData(c, endDateTime="", durationStr="1 Y",
                                        barSizeSetting="1 day", whatToShow="TRADES",
                                        useRTH=True, formatDate=1)
            if bars:
                out[s] = ([str(b.date) for b in bars], [float(b.close) for b in bars])
    finally:
        ib.disconnect()
    return out


def seed(data):
    st = {"equity_per_sleeve": EQUITY_PER_SLEEVE,
          "started": datetime.now(timezone.utc).date().isoformat(),
          "last_date": None, "sleeves": {}}
    for s in SYMBOLS:
        _, closes = data[s]
        px = closes[-1]
        bh_fill = px * (1 + (SLIPPAGE_BPS + HALF_SPREAD_BPS) / 10_000.0)
        bh_comm = commission((EQUITY_PER_SLEEVE - MIN_COMM) / bh_fill, EQUITY_PER_SLEEVE)
        st["sleeves"][s] = {"state": "CASH", "cash": EQUITY_PER_SLEEVE, "shares": 0.0,
                            "entry_px": 0.0, "bh_shares": (EQUITY_PER_SLEEVE - bh_comm) / bh_fill,
                            "round_trips": 0}
    return st


def process(st, data):
    """Advance the paper book by the latest completed daily bar."""
    latest = max(data[s][0][-1] for s in SYMBOLS if s in data)
    if st.get("last_date") == latest:
        _log(f"already processed {latest}; nothing to do")
        return st, False
    events = []
    for s in SYMBOLS:
        if s not in data:
            continue
        dates, closes = data[s]
        sl = st["sleeves"][s]
        inp = inputs_from_closes(closes)
        d = decide(inp, "LONG" if sl["state"] == "LONG" else "CASH")
        px = closes[-1]
        if d == ENTER and sl["state"] == "CASH":
            fill = px * (1 + (SLIPPAGE_BPS + HALF_SPREAD_BPS) / 10_000.0)
            comm = commission((sl["cash"]) / fill, sl["cash"])
            qty = (sl["cash"] - comm) / fill
            if qty > 0:
                sl["shares"] = qty; sl["cash"] -= qty * fill + comm
                sl["state"] = "LONG"; sl["entry_px"] = fill; sl["round_trips"] += 1
                events.append(f"{s} ENTER @ {fill:.2f}")
        elif d == EXIT and sl["state"] == "LONG":
            fill = px * (1 - (SLIPPAGE_BPS + HALF_SPREAD_BPS) / 10_000.0)
            proceeds = sl["shares"] * fill; comm = commission(sl["shares"], proceeds)
            sl["cash"] += proceeds - comm; sl["shares"] = 0.0
            sl["state"] = "CASH"; sl["entry_px"] = 0.0
            events.append(f"{s} EXIT @ {fill:.2f}")
    # mark to market + journal
    strat_eq = 0.0; bh_eq = 0.0; marks = {}
    for s in SYMBOLS:
        if s not in data:
            continue
        px = data[s][1][-1]; sl = st["sleeves"][s]
        se = sl["cash"] + sl["shares"] * px
        be = sl["bh_shares"] * px
        strat_eq += se; bh_eq += be
        marks[s] = {"state": sl["state"], "px": round(px, 2),
                    "strat": round(se, 2), "bh": round(be, 2)}
    st["last_date"] = latest
    rec = {"date": latest, "ts": datetime.now(timezone.utc).isoformat(),
           "events": events, "strat_equity": round(strat_eq, 2),
           "bh_equity": round(bh_eq, 2),
           "strat_ret_pct": round((strat_eq / (EQUITY_PER_SLEEVE * len(SYMBOLS)) - 1) * 100, 2),
           "bh_ret_pct": round((bh_eq / (EQUITY_PER_SLEEVE * len(SYMBOLS)) - 1) * 100, 2),
           "marks": marks}
    LOG_DIR.mkdir(exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    _log(f"processed {latest}: {', '.join(events) if events else 'no state changes'} | "
         f"paper strat {rec['strat_ret_pct']:+.2f}% vs B&H {rec['bh_ret_pct']:+.2f}%")
    return st, True


def print_status(st):
    if not st:
        print("no shadow state yet — run once to seed."); return
    print(f"REGIME SHADOW  started {st['started']}  last {st.get('last_date')}")
    tot_s = tot_b = 0.0
    for s in SYMBOLS:
        sl = st["sleeves"].get(s, {})
        print(f"  {s}: {sl.get('state'):<4} shares={sl.get('shares',0):.3f} "
              f"cash=${sl.get('cash',0):.0f} round_trips={sl.get('round_trips',0)}")


def main():
    if "--status" in sys.argv:
        print_status(load_state()); return 0
    if STOP_FILE.exists():
        _log("STOP_MEDIK present — shadow not running."); return 0
    try:
        data = fetch_daily()
    except Exception as exc:
        _log(f"data fetch failed ({type(exc).__name__}: {exc}); will retry next run"); return 0
    if len(data) < len(SYMBOLS):
        _log(f"only {len(data)}/{len(SYMBOLS)} symbols fetched; skipping to stay consistent"); return 0
    st = load_state()
    if st is None:
        st = seed(data); _log("seeded shadow paper book ($10k/sleeve, all CASH)")
    st, _ = process(st, data)
    save_state(st)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
