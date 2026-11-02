#!/usr/bin/env python3
"""Kraken spot trading tools for the kraken-trader skill.

The Kraken counterpart of the vendored trader-* stock skills: scan, entry,
plan (stop/target), replay (history + Monte Carlo, NET OF FEES), size, risk,
ticket/approve/place, flatten, alerts, status. Standard library only.

Kraken REST API (docs.kraken.com):
  public   GET  https://api.kraken.com/0/public/{AssetPairs,Ticker,OHLC}
  private  POST https://api.kraken.com/0/private/{Balance,TradeBalance,
                OpenOrders,TradeVolume,AddOrder,CancelOrder}
  Private calls are signed: API-Sign = base64(HMAC-SHA512(base64decode(secret),
  uri_path + SHA256(nonce + postdata))). Keys come ONLY from the environment
  variables KRAKEN_API_KEY / KRAKEN_API_SECRET and are never printed.

Safety model (see SKILL.md):
  * Long-only spot. No margin/leverage parameter is ever sent.
  * An order reaches Kraken only via `place`, which needs (1) a risk PASS for
    that exact order today, (2) a recorded explicit "yes" for that order, and
    (3) the person at the terminal typing YES after Kraken's validate-only
    preview. `place` refuses to run without an interactive terminal, so an
    agent driving this script through a non-interactive shell cannot submit.
  * The OHLC endpoint's last row is the still-forming bar; it is always
    dropped, so signals only ever use completed bars.

State lives in trader/kraken/ (override: $KRAKEN_TRADER_DIR); it is gitignored.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.kraken.com"
UA = "medik-kraken-trader/1.0"

DEFAULT_CONFIG = {
    "quote": "USD",                 # quote currency for scans and orders (USD or CAD)
    "account_value": None,          # used only when no API keys are set
    "cash": None,                   # used only when no API keys are set
    "fee_rate": 0.004,              # per side; replaced by your real taker fee when keys are set
    "max_loss_pct": 1.0,            # % of account value lost if the stop fills
    "max_position_pct": 20.0,       # % of account value in one coin
    "max_deployed_pct": 80.0,       # % of account value in coins combined
    "cash_buffer_pct": 20.0,        # % of account value always left in cash
    "min_reward_risk": 2.0,         # NET of fees on both legs
    "max_daily_loss_pct": 3.0,      # sum of max losses of today's (UTC) entries
    "max_trades_per_day": 3,
    "limit_buffer_pct": 0.3,        # buy limit this far above the plan entry (fills at the ask)
    "blocked": [],
    "approval_minutes": 15,         # a yes expires after this long
}


class KrakenError(Exception):
    pass


# --------------------------------------------------------------------------- files

def state_dir() -> str:
    d = os.environ.get("KRAKEN_TRADER_DIR") or os.path.join("trader", "kraken")
    os.makedirs(d, exist_ok=True)
    return d


def path(name: str) -> str:
    return os.path.join(state_dir(), name)


def read_json(name: str, default=None):
    try:
        with open(path(name), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(name: str, obj) -> str:
    p = path(name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    os.replace(tmp, p)
    return p


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(read_json("config.json", {}) or {})
    return cfg


def utcnow() -> datetime:
    override = os.environ.get("KRAKEN_TRADER_NOW")  # tests / replays: "2026-11-02T15:00:00"
    if override:
        return datetime.fromisoformat(override).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def today() -> str:
    return utcnow().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- HTTP

def _http(method: str, url: str, data: bytes | None = None, headers: dict | None = None) -> dict:
    """Single network choke point; tests replace it."""
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise KrakenError("HTTP %s from %s" % (e.code, url.split("?")[0]))
    except urllib.error.URLError as e:
        raise KrakenError("cannot reach Kraken (%s). If this is a cloud sandbox, its "
                          "network policy may block api.kraken.com; run it on your own machine."
                          % e.reason)


def public(endpoint: str, params: dict | None = None) -> dict:
    url = "%s/0/public/%s" % (API, endpoint)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    out = _http("GET", url)
    if out.get("error"):
        raise KrakenError("; ".join(out["error"]))
    return out["result"]


def sign(uri_path: str, postdata: str, nonce: str, secret_b64: str) -> str:
    sha = hashlib.sha256((nonce + postdata).encode()).digest()
    mac = hmac.new(base64.b64decode(secret_b64), uri_path.encode() + sha, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def have_keys() -> bool:
    return bool(os.environ.get("KRAKEN_API_KEY") and os.environ.get("KRAKEN_API_SECRET"))


def private(endpoint: str, params: dict | None = None) -> dict:
    if not have_keys():
        raise KrakenError("KRAKEN_API_KEY / KRAKEN_API_SECRET are not set in this terminal")
    uri_path = "/0/private/" + endpoint
    nonce = str(int(time.time() * 1000))
    body = dict(params or {})
    body["nonce"] = nonce
    postdata = urllib.parse.urlencode(body)
    headers = {
        "API-Key": os.environ["KRAKEN_API_KEY"],
        "API-Sign": sign(uri_path, postdata, nonce, os.environ["KRAKEN_API_SECRET"]),
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    }
    out = _http("POST", API + uri_path, postdata.encode(), headers)
    if out.get("error"):
        raise KrakenError("; ".join(out["error"]))
    return out["result"]


# --------------------------------------------------------------------------- market data

_PAIRS_CACHE: dict | None = None


def asset_pairs() -> dict:
    global _PAIRS_CACHE
    if _PAIRS_CACHE is None:
        _PAIRS_CACHE = public("AssetPairs")
    return _PAIRS_CACHE


def pair_info(pair: str) -> tuple[str, dict]:
    """Resolve 'BTCUSD', 'XBTUSD', 'BTC/USD' or 'XXBTZUSD' to (key, info)."""
    want = pair.upper().replace("-", "/")
    alts = {want, want.replace("/", ""), want.replace("BTC", "XBT"), want.replace("BTC", "XBT").replace("/", "")}
    for key, info in asset_pairs().items():
        names = {key.upper(), str(info.get("altname", "")).upper(), str(info.get("wsname", "")).upper(),
                 str(info.get("wsname", "")).upper().replace("/", "")}
        if names & alts:
            return key, info
    raise KrakenError("unknown Kraken pair %r" % pair)


def ohlc(pair: str, interval: int) -> list[dict]:
    """Completed bars only: Kraken's last row is the bar still forming."""
    key, _ = pair_info(pair)
    res = public("OHLC", {"pair": key, "interval": interval})
    rows = next(v for k, v in res.items() if k != "last")
    bars = [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
             "c": float(r[4]), "v": float(r[6])} for r in rows]
    return bars[:-1]


def quote(pair: str) -> dict:
    key, _ = pair_info(pair)
    t = public("Ticker", {"pair": key})[key]
    return {"pair": key, "ask": float(t["a"][0]), "bid": float(t["b"][0]), "last": float(t["c"][0]),
            "vol24": float(t["v"][1]), "open24": float(t["o"]), "time": utcnow().isoformat()}


def round_down(x: float, decimals: int) -> float:
    q = 10 ** decimals
    return math.floor(x * q + 1e-9) / q


def round_up(x: float, decimals: int) -> float:
    q = 10 ** decimals
    return math.ceil(x * q - 1e-9) / q


# --------------------------------------------------------------------------- indicators

def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(bars, bars[1:])]
    return sum(trs[-n:]) / n


def breakout(bars, lookback):
    """Does the last completed bar CLOSE above the prior `lookback` bars' high?"""
    if len(bars) < lookback + 1:
        return None
    window = bars[-lookback - 1:-1]
    hi, lo = max(b["h"] for b in window), min(b["l"] for b in window)
    last = bars[-1]
    return {"triggered": last["c"] > hi, "range_high": hi, "range_low": lo,
            "close": last["c"], "bar_time": last["t"], "pct_from_high": (last["c"] / hi - 1) * 100}


# --------------------------------------------------------------------------- fees and R

def net_risk(entry, stop, f):
    return entry * (1 + f) - stop * (1 - f)


def net_reward(entry, target, f):
    return target * (1 - f) - entry * (1 + f)


def target_for_r(entry, stop, f, r):
    return (entry * (1 + f) + r * net_risk(entry, stop, f)) / (1 - f)


# --------------------------------------------------------------------------- account

def fee_rate(cfg, pair_key=None) -> tuple[float, str]:
    if have_keys() and pair_key:
        try:
            tv = private("TradeVolume", {"pair": pair_key})
            fee = float(tv["fees"][pair_key]["fee"]) / 100
            return fee, "your Kraken taker fee"
        except (KrakenError, KeyError, ValueError):
            pass
    return float(cfg["fee_rate"]), "config fee_rate (assumed)"


def account(cfg) -> dict:
    """Value, cash and holdings in the quote currency."""
    q = cfg["quote"].upper()
    if not have_keys():
        if cfg.get("account_value") is None:
            raise KrakenError("no API keys and no account_value in trader/kraken/config.json")
        return {"value": float(cfg["account_value"]),
                "cash": float(cfg["cash"] if cfg.get("cash") is not None else cfg["account_value"]),
                "holdings": read_json("holdings.json", {}) or {}, "source": "config (no API keys)"}
    tb = private("TradeBalance", {"asset": "Z" + q})
    bal = private("Balance")
    cash = sum(float(v) for k, v in bal.items() if k.split(".")[0] in ("Z" + q, q))
    holdings = {k: float(v) for k, v in bal.items() if float(v) > 0 and k.split(".")[0] not in ("Z" + q, q)}
    return {"value": float(tb["eb"]), "cash": cash, "holdings": holdings, "source": "Kraken"}


def held_volume(acct, pair_info_):
    base = pair_info_.get("base", "")
    return sum(v for k, v in acct["holdings"].items() if k.split(".")[0] == base)


# --------------------------------------------------------------------------- commands: data

def cmd_scan(a, cfg):
    q = cfg["quote"].upper()
    pairs = asset_pairs()
    tick = public("Ticker")
    rows = []
    for key, t in tick.items():
        info = pairs.get(key)
        if not info or key.endswith(".d") or info.get("status", "online") != "online":
            continue
        if info.get("quote") not in ("Z" + q, q):
            continue
        rows.append((float(t["v"][1]) * float(t["c"][0]), key))
    rows.sort(reverse=True)
    out = []
    for vol_quote, key in rows[:a.top]:
        try:
            bars = ohlc(key, a.interval)
        except KrakenError as e:
            print("  skip %s: %s" % (key, e))
            continue
        closes = [b["c"] for b in bars]
        bo = breakout(bars, a.lookback)
        r_now, r_prev = rsi(closes), rsi(closes[:-1])
        s50 = sma(closes, 50)
        setups = []
        if bo and bo["triggered"]:
            setups.append("breakout")
        if s50 and closes[-1] > s50:
            setups.append("above_sma50")
        if r_now is not None and r_now < 30:
            setups.append("oversold")
        if r_now is not None and r_prev is not None and r_prev < 30 <= r_now:
            setups.append("oversold_bounce")
        out.append({"pair": key, "wsname": pairs[key].get("wsname"), "close": closes[-1],
                    "vol24_quote": round(vol_quote), "rsi14": None if r_now is None else round(r_now, 1),
                    "setups": setups, "range": bo})
    hits = [r for r in out if r["setups"]]
    write_json("watchlist.json", {"time": utcnow().isoformat(), "interval": a.interval,
                                  "lookback": a.lookback, "scanned": len(out), "rows": out})
    print("Scanned the %d most-traded %s pairs on %d-minute completed bars." % (len(out), q, a.interval))
    for r in sorted(hits, key=lambda r: -r["vol24_quote"]):
        print("  %-10s %-12s close %-12g RSI %-5s %s" % (r["wsname"], r["pair"], r["close"], r["rsi14"],
                                                      ", ".join(r["setups"])))
    print("A scan is a list of charts matching a rule, not a list of things to buy. -> watchlist.json")


def cmd_quote(a, cfg):
    qt = quote(a.pair)
    write_json("prices.json", {**(read_json("prices.json", {}) or {}), qt["pair"]: qt})
    print("%s  bid %g  ask %g  last %g  24h %+.2f%%" % (qt["pair"], qt["bid"], qt["ask"], qt["last"],
                                                       (qt["last"] / qt["open24"] - 1) * 100))


def cmd_entry(a, cfg):
    key, _ = pair_info(a.pair)
    bars = ohlc(key, a.interval)
    bo = breakout(bars, a.lookback)
    if bo is None:
        raise KrakenError("not enough bars")
    status = "TRIGGERED" if bo["triggered"] else "waiting"
    t = datetime.fromtimestamp(bo["bar_time"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print("%s %s: last completed %d-min bar (%s) closed %g; range of prior %d bars %g - %g (%+.2f%% vs high)"
          % (status, key, a.interval, t, bo["close"], a.lookback, bo["range_low"], bo["range_high"],
             bo["pct_from_high"]))
    if bo["triggered"]:
        write_json("entry.json", {"pair": key, "entry": bo["close"], "interval": a.interval,
                                  "lookback": a.lookback, **bo})
        print("-> entry.json. Next: plan the stop and target.")


# --------------------------------------------------------------------------- plan

def make_plan(pair_key, info, entry, range_low, range_high, atr_val, stop_rule, r, f, buffer_pct=0.0):
    """Target is set so the NET reward:risk is `r` even at the worst-case fill, the buy
    limit (entry + buffer); risk checks measure from that limit, not the hoped-for entry."""
    dec = int(info.get("pair_decimals", 5))
    worst = round_up(entry * (1 + buffer_pct / 100), dec)
    if stop_rule == "range-mid":
        stop = (range_low + range_high) / 2
    elif stop_rule == "range-low":
        stop = range_low
    elif stop_rule.startswith("pct:"):
        stop = entry * (1 - float(stop_rule[4:]) / 100)
    elif stop_rule.startswith("atr:"):
        if not atr_val:
            raise KrakenError("no ATR available")
        stop = entry - float(stop_rule[4:]) * atr_val
    else:
        stop = float(stop_rule)
    stop = round_down(stop, dec)
    if stop >= entry:
        raise KrakenError("stop %g is not below the entry %g" % (stop, entry))
    if (entry - stop) / entry < 2 * f:
        raise KrakenError("stop is only %.2f%% away; round-trip fees alone are %.2f%%. Too tight."
                          % ((entry - stop) / entry * 100, 2 * f * 100))
    target = round_up(target_for_r(worst, stop, f, r), dec)
    return {"pair": pair_key, "entry": entry, "entry_limit": worst, "stop": stop, "target": target,
            "fee_rate": f, "gross_r": (target - worst) / (worst - stop),
            "net_r": net_reward(worst, target, f) / net_risk(worst, stop, f),
            "stop_rule": stop_rule, "target_r_net": r}


def cmd_plan(a, cfg):
    e = read_json("entry.json", {}) or {}
    pair = a.pair or e.get("pair")
    if not pair:
        raise KrakenError("give --pair (no entry.json)")
    key, info = pair_info(pair)
    entry = a.entry if a.entry is not None else e.get("entry")
    if entry is None:
        raise KrakenError("give --entry (no entry.json)")
    rl, rh = e.get("range_low"), e.get("range_high")
    av = None
    if a.stop in ("range-mid", "range-low") and (rl is None or e.get("pair") != key):
        raise KrakenError("range stops need an entry.json for this pair; use pct:N, atr:K or a price")
    if a.stop.startswith("atr:"):
        av = atr(ohlc(key, e.get("interval", 60)))
    f, src = fee_rate(cfg, key)
    p = make_plan(key, info, float(entry), rl, rh, av, a.stop, a.r, f, cfg["limit_buffer_pct"])
    p.update({"interval": e.get("interval", 60), "lookback": e.get("lookback", 20),
              "horizon_bars": a.horizon, "created": utcnow().isoformat(), "fee_source": src})
    write_json("plan.json", p)
    print("PLAN %s  entry %g (buy limit %g)  stop %g  target %g" % (key, p["entry"], p["entry_limit"],
                                                                   p["stop"], p["target"]))
    print("  fees %.2f%% a side (%s); at the buy limit, reward:risk %.2f gross, %.2f NET of both fees"
          % (f * 100, src, p["gross_r"], p["net_r"]))
    print("  exit at market if neither hits within %d bars. -> plan.json. Next: replay." % a.horizon)


# --------------------------------------------------------------------------- replay

def walk(bars, start, entry, stop, target, horizon):
    """From bar index `start` (the fill bar), stop-first when both are touched in one bar."""
    end = min(len(bars), start + horizon)
    for j in range(start, end):
        b = bars[j]
        if b["l"] <= stop:
            return "stop", stop, j
        if b["h"] >= target:
            return "target", target, j
    j = end - 1
    return "time", bars[j]["c"], j


def history(bars, lookback, stop_rule, r, f, horizon):
    """Re-run the breakout rule on every past bar. Signal on a completed close, fill at
    the NEXT bar's open (no lookahead), one trade at a time."""
    trades, i = [], lookback
    while i < len(bars) - 1:
        bo = breakout(bars[:i + 1], lookback)
        if not bo or not bo["triggered"]:
            i += 1
            continue
        entry = bars[i + 1]["o"]
        if stop_rule == "range-mid":
            stop = (bo["range_low"] + bo["range_high"]) / 2
        elif stop_rule == "range-low":
            stop = bo["range_low"]
        elif stop_rule.startswith("pct:"):
            stop = entry * (1 - float(stop_rule[4:]) / 100)
        elif stop_rule.startswith("atr:"):
            av = atr(bars[:i + 1])
            stop = entry - float(stop_rule[4:]) * av if av else entry * 0.97
        else:
            stop = entry * 0.97
        if stop >= entry or (entry - stop) / entry < 2 * f:
            i += 1
            continue
        target = target_for_r(entry, stop, f, r)
        how, px, j = walk(bars, i + 1, entry, stop, target, horizon)
        nr = net_risk(entry, stop, f)
        pnl = px * (1 - f) - entry * (1 + f)
        trades.append({"t": bars[i]["t"], "entry": entry, "exit": px, "how": how,
                       "r_net": pnl / nr, "ret_net": pnl / (entry * (1 + f)),
                       "ret_gross": px / entry - 1})
        i = j + 1
    return trades


def monte_carlo(bars, entry, stop, target, horizon, n, seed):
    """Rebuild the next `horizon` bars n times from the pair's own bar shapes."""
    shapes = [(b["h"] / b["o"], b["l"] / b["o"], b["c"] / b["o"]) for b in bars if b["o"] > 0]
    rng = random.Random(seed)
    tally = {"target": 0, "stop": 0, "time": 0}
    for _ in range(n):
        p = entry
        res = "time"
        for _ in range(horizon):
            rh, rl, rc = shapes[rng.randrange(len(shapes))]
            if p * rl <= stop:
                res = "stop"
                break
            if p * rh >= target:
                res = "target"
                break
            p *= rc
        tally[res] += 1
    return {k: v / n for k, v in tally.items()}


def summarize(trades):
    if not trades:
        return None
    wins = [t for t in trades if t["ret_net"] > 0]
    gp = sum(t["ret_net"] for t in wins)
    gl = -sum(t["ret_net"] for t in trades if t["ret_net"] <= 0)
    return {"n": len(trades), "win_rate": len(wins) / len(trades),
            "avg_r_net": sum(t["r_net"] for t in trades) / len(trades),
            "avg_ret_net": sum(t["ret_net"] for t in trades) / len(trades),
            "avg_ret_gross": sum(t["ret_gross"] for t in trades) / len(trades),
            "profit_factor_net": (gp / gl) if gl > 0 else float("inf"),
            "by_exit": {k: sum(1 for t in trades if t["how"] == k) for k in ("target", "stop", "time")}}


def cmd_replay(a, cfg):
    p = read_json("plan.json")
    if not p:
        raise KrakenError("no plan.json; run plan first")
    bars = ohlc(p["pair"], p["interval"])
    mc = monte_carlo(bars, p["entry"], p["stop"], p["target"], p["horizon_bars"], a.n, a.seed)
    hist = summarize(history(bars, p["lookback"], p["stop_rule"], p["target_r_net"], p["fee_rate"],
                             p["horizon_bars"]))
    span_days = (bars[-1]["t"] - bars[0]["t"]) / 86400 if bars else 0
    verdict = "NO HISTORY: the rule never fired in this window. Unknown, so not tradeable."
    if hist:
        if hist["avg_r_net"] <= 0:
            verdict = ("NEGATIVE after fees: %.2fR a trade over %d past trades. Do not trade this live."
                       % (hist["avg_r_net"], hist["n"]))
        elif hist["n"] < 30:
            verdict = ("Positive (%.2fR) but only %d trades: too few to tell skill from luck. Paper only."
                       % (hist["avg_r_net"], hist["n"]))
        else:
            verdict = ("Positive after fees in this window: %.2fR over %d trades. One window is one regime; "
                       "treat as evidence, not proof." % (hist["avg_r_net"], hist["n"]))
    out = {"plan": p, "monte_carlo": mc, "history": hist, "bars": len(bars), "span_days": span_days,
           "verdict": verdict, "seed": a.seed, "time": utcnow().isoformat()}
    write_json("replay.json", out)
    print("REPLAY %s  %d paths of %d bars, seed %d" % (p["pair"], a.n, p["horizon_bars"], a.seed))
    print("  target first %.1f%%  stop first %.1f%%  neither %.1f%%"
          % (mc["target"] * 100, mc["stop"] * 100, mc["time"] * 100))
    if hist:
        print("  history (%d bars, %.0f days, fill at next open, fees both legs): %d trades, win %.0f%%,"
              " avg %.2fR net, avg %+.2f%% net vs %+.2f%% gross, PF %.2f"
              % (len(bars), span_days, hist["n"], hist["win_rate"] * 100, hist["avg_r_net"],
                 hist["avg_ret_net"] * 100, hist["avg_ret_gross"] * 100, hist["profit_factor_net"]))
    print("  VERDICT: " + verdict)
    print("  Kraken serves at most 720 bars per interval: a short window. A simulation, not a prediction.")


# --------------------------------------------------------------------------- size

def build_order(p, info, acct, cfg, f):
    entry_limit = p["entry_limit"]
    value, cash = acct["value"], acct["cash"]
    per_unit_risk = net_risk(entry_limit, p["stop"], f)
    unit_cost = entry_limit * (1 + f)
    caps = {
        "max loss per trade": value * cfg["max_loss_pct"] / 100 / per_unit_risk,
        "position cap": value * cfg["max_position_pct"] / 100 / unit_cost,
        "cash after buffer": max(0.0, cash - value * cfg["cash_buffer_pct"] / 100) / unit_cost,
    }
    binding = min(caps, key=caps.get)
    vol = round_down(caps[binding], int(info.get("lot_decimals", 8)))
    ordermin = float(info.get("ordermin", 0) or 0)
    costmin = float(info.get("costmin", 0) or 0)
    if vol <= 0 or vol < ordermin or vol * entry_limit < costmin:
        raise KrakenError("REFUSED: %s allows %g, below Kraken's minimum (%g units / %g cost). "
                          "Not a smaller order: skip the trade or use a closer stop."
                          % (binding, vol, ordermin, costmin))
    order = {"pair": p["pair"], "type": "buy", "ordertype": "limit", "price": str(entry_limit),
             "volume": ("%." + str(int(info.get("lot_decimals", 8))) + "f") % vol,
             "close[ordertype]": "stop-loss", "close[price]": str(p["stop"])}
    meta = {"target": p["target"], "stop": p["stop"], "fee_rate": f, "binding": binding,
            "max_loss": vol * per_unit_risk, "cost": vol * unit_cost, "net_r": p["net_r"],
            "why": "breakout plan %s: %g units, bound by %s" % (p["pair"], vol, binding)}
    return order, meta


def cmd_size(a, cfg):
    p = read_json("plan.json")
    if not p:
        raise KrakenError("no plan.json")
    key, info = pair_info(p["pair"])
    acct = account(cfg)
    order, meta = build_order(p, info, acct, cfg, p["fee_rate"])
    write_json("orders.json", {"orders": [{"order": order, "meta": meta}], "created": utcnow().isoformat()})
    print("ORDER buy %s %s limit %s, attached stop-loss %s; target %g (alert, not attached)"
          % (order["volume"], key, order["price"], order["close[price]"], meta["target"]))
    print("  cost %.2f, max loss at the stop %.2f incl. fees (%s, account %.2f from %s)"
          % (meta["cost"], meta["max_loss"], meta["binding"], acct["value"], acct["source"]))
    print("-> orders.json. Next: risk.")


def cmd_flatten(a, cfg):
    key, info = pair_info(a.pair)
    acct = account(cfg)
    vol = a.volume if a.volume is not None else held_volume(acct, info)
    vol = round_down(vol, int(info.get("lot_decimals", 8)))
    if vol <= 0:
        raise KrakenError("nothing held in %s" % key)
    qt = quote(key)
    px = round_down(qt["bid"] * (1 - cfg["limit_buffer_pct"] / 100), int(info.get("pair_decimals", 5)))
    order = {"pair": key, "type": "sell", "ordertype": "limit", "price": str(px),
             "volume": ("%." + str(int(info.get("lot_decimals", 8))) + "f") % vol}
    write_json("orders.json", {"orders": [{"order": order, "meta": {"why": "flatten %s" % key, "exit": True}}],
                               "created": utcnow().isoformat()})
    print("ORDER sell %s %s limit %g (%.2f%% under the bid %g; fills at the bid). -> orders.json"
          % (order["volume"], key, px, cfg["limit_buffer_pct"], qt["bid"]))
    print("Any attached stop-loss still holds these coins: cancel it first (orders, then cancel).")


# --------------------------------------------------------------------------- risk

def fingerprint(order: dict) -> str:
    return hashlib.sha256(json.dumps(order, sort_keys=True).encode()).hexdigest()[:16]


def broker_log():
    return read_json("broker.json", {"entries": []}) or {"entries": []}


def check_order(order, meta, acct, cfg, info, log, batch_seen):
    reasons, warns = [], []
    is_exit = order.get("type") == "sell"
    for k in ("pair", "type", "ordertype", "volume"):
        if not order.get(k):
            reasons.append("format: missing %s" % k)
    if order.get("type") not in ("buy", "sell"):
        reasons.append("format: type must be buy or sell")
    if any(k in order for k in ("leverage", "reduce_only")):
        reasons.append("margin: leverage is never allowed")
    if order.get("ordertype") == "limit" and not order.get("price"):
        reasons.append("format: limit order with no price")
    vol = float(order.get("volume") or 0)
    held = held_volume(acct, info)
    if is_exit:
        if vol > held + 1e-12:
            reasons.append("short: selling %g but only %g held (spot, long-only)" % (vol, held))
        return reasons, warns
    price = float(order.get("price") or 0)
    f = float(meta.get("fee_rate", cfg["fee_rate"]))
    if order["pair"] in cfg["blocked"] or info.get("wsname") in cfg["blocked"]:
        reasons.append("blocked: %s is on your blocked list" % order["pair"])
    stop = order.get("close[price]")
    if order.get("close[ordertype]") != "stop-loss" or not stop:
        reasons.append("stop: every entry needs an attached stop-loss")
        return reasons, warns
    stop = float(stop)
    if stop >= price:
        reasons.append("stop: %g is not below the entry %g" % (stop, price))
        return reasons, warns
    value = acct["value"]
    loss = vol * net_risk(price, stop, f)
    if loss > value * cfg["max_loss_pct"] / 100 + 1e-9:
        reasons.append("loss: %.2f at the stop incl. fees, over %.2f (%.1f%% of %.2f)"
                       % (loss, value * cfg["max_loss_pct"] / 100, cfg["max_loss_pct"], value))
    target = meta.get("target")
    if target is None:
        reasons.append("reward:risk: no target")
    else:
        rr = net_reward(price, float(target), f) / net_risk(price, stop, f)
        if rr < cfg["min_reward_risk"] - 1e-9:
            reasons.append("reward:risk: %.2f net of fees, under %.2f" % (rr, cfg["min_reward_risk"]))
    cost = vol * price * (1 + f)
    if cost > value * cfg["max_position_pct"] / 100 + 1e-9:
        reasons.append("position: %.2f is over %.0f%% of the account" % (cost, cfg["max_position_pct"]))
    if acct["cash"] - cost < value * cfg["cash_buffer_pct"] / 100 - 1e-9:
        reasons.append("cash: would leave %.2f, under the %.0f%% buffer" % (acct["cash"] - cost,
                                                                         cfg["cash_buffer_pct"]))
    deployed = value - acct["cash"] + cost
    if deployed > value * cfg["max_deployed_pct"] / 100 + 1e-9:
        reasons.append("deployed: %.0f%% of the account, over %.0f%%" % (deployed / value * 100,
                                                                      cfg["max_deployed_pct"]))
    todays = [e for e in log["entries"] if e.get("date") == today() and e.get("status") == "placed"
              and not e.get("exit")]
    used = sum(float(e.get("max_loss", 0)) for e in todays)
    if used + loss > value * cfg["max_daily_loss_pct"] / 100 + 1e-9:
        reasons.append("daily loss: %.2f already at risk today + %.2f is over %.2f"
                       % (used, loss, value * cfg["max_daily_loss_pct"] / 100))
    if len(todays) >= cfg["max_trades_per_day"]:
        reasons.append("trades: %d entries already today (max %d)" % (len(todays), cfg["max_trades_per_day"]))
    if order["pair"] in batch_seen or any(e.get("pair") == order["pair"] for e in todays):
        reasons.append("duplicate: already an entry in %s today" % order["pair"])
    batch_seen.add(order["pair"])
    if f == DEFAULT_CONFIG["fee_rate"] and not have_keys():
        warns.append("fee rate is an assumption (no API keys); check your Kraken fee tier")
    return reasons, warns


def cmd_risk(a, cfg):
    doc = read_json("orders.json")
    if not doc or not doc.get("orders"):
        raise KrakenError("no orders.json")
    acct = account(cfg)
    log = broker_log()
    verdicts, seen, blocked = [], set(), 0
    for i, item in enumerate(doc["orders"], 1):
        order, meta = item["order"], item.get("meta", {})
        _, info = pair_info(order["pair"])
        reasons, warns = check_order(order, meta, acct, cfg, info, log, seen)
        ok = not reasons
        blocked += not ok
        verdicts.append({"index": i, "fingerprint": fingerprint(order), "date": today(),
                         "verdict": "PASS" if ok else "BLOCK", "reasons": reasons, "warnings": warns})
        print("%s #%d %s %s %s @ %s" % ("PASS " if ok else "BLOCK", i, order["type"], order["volume"],
                                       order["pair"], order.get("price", "market")))
        for r in reasons:
            print("        - " + r)
        for w in warns:
            print("        ! " + w)
    if not a.no_write:
        write_json("risk.json", {"verdicts": verdicts, "time": utcnow().isoformat()})
    print("%d passed, %d blocked. A PASS is a rules check, not a recommendation." % (len(verdicts) - blocked,
                                                                                  blocked))
    return 1 if blocked else 0


# --------------------------------------------------------------------------- tickets

# "yes", optionally followed by please / place it / send it / the pair's own name.
# Any other named word (a different coin, "to all") fails the check in cmd_approve.
YES = re.compile(r"^\s*(yes|y)(,?\s+[A-Za-z/]{2,12})*\s*[.!]?\s*$", re.I)


def ready(index):
    doc = read_json("orders.json") or {}
    orders = doc.get("orders", [])
    if not 1 <= index <= len(orders):
        raise KrakenError("no order #%d" % index)
    item = orders[index - 1]
    fp = fingerprint(item["order"])
    rv = read_json("risk.json", {}) or {}
    v = next((v for v in rv.get("verdicts", []) if v["index"] == index), None)
    ok = bool(v and v["verdict"] == "PASS" and v["fingerprint"] == fp and v["date"] == today())
    return item, fp, ok


def cmd_ticket(a, cfg):
    doc = read_json("orders.json") or {}
    for i, item in enumerate(doc.get("orders", []), 1):
        o, m = item["order"], item.get("meta", {})
        _, fp, ok = ready(i)
        print("ORDER %d, LIVE Kraken spot account (Kraken has no paper mode for spot)" % i)
        print("  %s %s %s, %s %s" % (o["type"].upper(), o["volume"], o["pair"], o["ordertype"],
                                     o.get("price", "")))
        if o.get("close[price]"):
            print("  attached stop-loss %s; target %s is an alert, not an order" % (o["close[price]"],
                                                                                 m.get("target")))
            print("  max loss about %.2f incl. fees if the stop fills at its price (it can fill worse)"
                  % m.get("max_loss", 0))
        print("  why: %s" % m.get("why", ""))
        print("  %s" % ("READY: risk PASS today, waiting for your yes" if ok
                        else "NOT READY: run risk (an edited or stale order needs a fresh PASS)"))
        print("  fingerprint %s" % fp)


def cmd_approve(a, cfg):
    item, fp, ok = ready(a.index)
    if not ok:
        raise KrakenError("order #%d is not READY; run risk first" % a.index)
    reply = a.reply or ""
    base = item["order"]["pair"]
    _, info = pair_info(base)
    names = " ".join([base, str(info.get("altname", "")), str(info.get("wsname", ""))]).upper()
    words = set(re.findall(r"[A-Z]{2,10}", reply.upper())) - {"YES", "PLEASE", "PLACE", "SEND", "IT"}
    named_other = any(w.replace("BTC", "XBT") not in names for w in words)
    if not YES.match(reply) or "ALL" in reply.upper() or named_other:
        print("Not recorded: %r is not an explicit yes for order #%d. Ask again." % (reply, a.index))
        return 1
    log = broker_log()
    log["entries"].append({"kind": "approval", "index": a.index, "fingerprint": fp, "reply": reply,
                           "time": utcnow().isoformat(), "date": today(), "pair": base})
    write_json("broker.json", log)
    print("Recorded your yes for order #%d (%s). Now run, in YOUR terminal:\n"
          "  python \"%s\" place --index %d" % (a.index, fp, os.path.abspath(__file__), a.index))
    return 0


def approval_for(fp, cfg):
    cutoff = utcnow().timestamp() - cfg["approval_minutes"] * 60
    for e in reversed(broker_log()["entries"]):
        if e.get("kind") == "approval" and e.get("fingerprint") == fp:
            return datetime.fromisoformat(e["time"]).timestamp() >= cutoff
    return False


def cmd_place(a, cfg, stdin=None):
    stdin = stdin or sys.stdin
    item, fp, ok = ready(a.index)
    if not ok:
        raise KrakenError("order #%d has no risk PASS today for this exact order" % a.index)
    if not approval_for(fp, cfg):
        raise KrakenError("no recorded yes for order #%d in the last %d minutes (approve first)"
                          % (a.index, cfg["approval_minutes"]))
    if not stdin.isatty():
        raise KrakenError("place must be run by you in an interactive terminal; it will not submit "
                          "from a non-interactive shell")
    order = item["order"]
    preview = private("AddOrder", {**order, "validate": "true"})
    print("Kraken validated (NOT submitted): %s" % preview.get("descr", {}))
    print("Type YES to submit this LIVE order, anything else to cancel: ", end="", flush=True)
    if stdin.readline().strip() != "YES":
        print("Cancelled. Nothing was sent.")
        return 1
    res = private("AddOrder", order)
    log = broker_log()
    log["entries"].append({"kind": "order", "status": "placed", "index": a.index, "fingerprint": fp,
                           "txid": res.get("txid"), "descr": res.get("descr"), "pair": order["pair"],
                           "max_loss": item.get("meta", {}).get("max_loss", 0),
                           "exit": order["type"] == "sell", "time": utcnow().isoformat(), "date": today()})
    write_json("broker.json", log)
    print("SUBMITTED %s. txid %s. Check it in Kraken; a limit may rest unfilled." % (res.get("descr"),
                                                                                   res.get("txid")))
    return 0


def cmd_cancel(a, cfg, stdin=None):
    stdin = stdin or sys.stdin
    if not stdin.isatty():
        raise KrakenError("cancel must be run by you in an interactive terminal")
    print("Type YES to cancel %s: " % a.txid, end="", flush=True)
    if stdin.readline().strip() != "YES":
        print("Nothing cancelled.")
        return 1
    print(private("CancelOrder", {"txid": a.txid}))
    return 0


# --------------------------------------------------------------------------- status

def cmd_orders(a, cfg):
    oo = private("OpenOrders").get("open", {})
    if not oo:
        print("No open orders on Kraken.")
    for txid, o in oo.items():
        print("%s  %s  vol %s  filled %s  status %s" % (txid, o["descr"]["order"], o["vol"], o["vol_exec"],
                                                       o["status"]))


def cmd_alerts(a, cfg):
    p = read_json("plan.json")
    if not p:
        raise KrakenError("no plan.json")
    qt = quote(p["pair"])
    px = qt["last"]
    if px <= p["stop"]:
        msg = "STOP %s: last %g <= stop %g. The attached stop-loss should have fired; check orders." % (
            p["pair"], px, p["stop"])
    elif px >= p["target"]:
        msg = "TARGET %s: last %g >= target %g. Target is not attached: flatten to take it." % (
            p["pair"], px, p["target"])
    else:
        msg = "OK %s: last %g, %.2f%% above stop %g, %.2f%% below target %g" % (
            p["pair"], px, (px / p["stop"] - 1) * 100, p["stop"], (p["target"] / px - 1) * 100, p["target"])
    print(msg)
    with open(path("alerts.log"), "a", encoding="utf-8") as f:
        f.write("%s %s\n" % (utcnow().isoformat(), msg))


def cmd_status(a, cfg):
    log = broker_log()
    todays = [e for e in log["entries"] if e.get("date") == today() and e.get("status") == "placed"]
    try:
        acct = account(cfg)
        print("Account %.2f %s (%s), cash %.2f" % (acct["value"], cfg["quote"], acct["source"], acct["cash"]))
        for k, v in acct["holdings"].items():
            print("  %s %g" % (k, v))
    except KrakenError as e:
        print("Account: %s" % e)
    print("Today (UTC %s): %d orders placed, %.2f max loss committed" % (
        today(), len(todays), sum(float(e.get("max_loss", 0)) for e in todays if not e.get("exit"))))
    for name in ("plan.json", "replay.json", "risk.json"):
        d = read_json(name)
        if d and name == "replay.json":
            print("Last replay: " + d.get("verdict", ""))
        elif d and name == "plan.json":
            print("Plan: %s entry %g stop %g target %g" % (d["pair"], d["entry"], d["stop"], d["target"]))


# --------------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(prog="kraken.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--top", type=int, default=30)
    s.add_argument("--interval", type=int, default=60, choices=[1, 5, 15, 30, 60, 240, 1440])
    s.add_argument("--lookback", type=int, default=20)
    s = sub.add_parser("quote")
    s.add_argument("pair")
    s = sub.add_parser("entry")
    s.add_argument("pair")
    s.add_argument("--interval", type=int, default=60, choices=[1, 5, 15, 30, 60, 240, 1440])
    s.add_argument("--lookback", type=int, default=20)
    s = sub.add_parser("plan")
    s.add_argument("--pair")
    s.add_argument("--entry", type=float)
    s.add_argument("--stop", default="range-mid", help="range-mid | range-low | pct:N | atr:K | <price>")
    s.add_argument("--r", type=float, default=2.0, help="target reward:risk NET of fees")
    s.add_argument("--horizon", type=int, default=24, help="bars before a time exit")
    s = sub.add_parser("replay")
    s.add_argument("--n", type=int, default=10000)
    s.add_argument("--seed", type=int, default=19)
    sub.add_parser("size")
    s = sub.add_parser("flatten")
    s.add_argument("pair")
    s.add_argument("--volume", type=float)
    s = sub.add_parser("risk")
    s.add_argument("--no-write", action="store_true")
    sub.add_parser("ticket")
    s = sub.add_parser("approve")
    s.add_argument("--index", type=int, required=True)
    s.add_argument("--reply", required=True)
    s = sub.add_parser("place")
    s.add_argument("--index", type=int, required=True)
    s = sub.add_parser("cancel")
    s.add_argument("txid")
    sub.add_parser("orders")
    sub.add_parser("alerts")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    cfg = load_config()
    fn = globals()["cmd_" + a.cmd]
    try:
        rc = fn(a, cfg)
    except KrakenError as e:
        print("ERROR: %s" % e)
        return 2
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
