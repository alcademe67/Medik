"""MEDIK ETF ACTIVE LIVE — scan the ETF universe and trade the best setup.

    MEDIK_ETF_LIVE=true python examples/medik_etf_live.py

No symbols are required: the universe is fixed in strategy.medik_etf. The
process RUNS CONTINUOUSLY through the regular session, scanning every
SCAN_INTERVAL_SEC, holding at most one position at a time.

LIVE MODE IS EXPLICIT, AND AUTHORISES AUTOMATIC SUBMISSION
    Orders are sent only when MEDIK_ETF_LIVE is set to exactly "true". Any
    other value — including unset — prints LIVE ETF TRADING DISABLED and
    refuses to send. Live mode is never inferred from a missing variable.

    Once enabled, qualifying orders are submitted AUTOMATICALLY with no
    per-order human approval. This is an owner-authorised exception to the
    repo's general draft-and-approve policy, scoped to this strategy alone
    and recorded in CLAUDE.md; every other order path still requires a human.

    The approval step is REPLACED, not removed. strategy.medik_etf's
    authorize_order() runs a deterministic checklist — live flag, connection,
    account data, buying power, market data, setup validity, whole-share
    size, stop, target, reward/risk, risk ceiling, capital utilisation,
    conflicting position, conflicting order, duplicate suppression, session
    gates — and an order is sent only when EVERY check passes. The function
    is pure, so identical inputs always give an identical decision. No model
    output authorises a trade.

EVERY TRADE IS A BUY
    The account cannot short. Bearish views are expressed by buying the
    inverse fund (SQQQ, SOXS, TZA, LABD, FAZ, ERY), each scored on its own
    price action rather than as a mechanical inversion of its bull twin.

BRACKET PROTECTION IS MANDATORY
    A position is not considered protected until the parent, stop and target
    are all acknowledged by IBKR. If the parent fills and either protective
    leg fails, the program attempts to re-establish protection; failing that
    it flattens the position with a marketable order, logs the exact IBKR
    error, and disables further entries for the run.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import MarketOrder, Stock

from ibkr.accounts import belongs_to, order_belongs_to
from ibkr.accounts import resolve_account as _resolve_account
from ibkr.client import IBKRClient
from ibkr.cpapi import ClientPortalQuotes, CpApiError
from strategy.medik_etf import (
    ETF_UNIVERSE,
    MAX_CAPITAL_UTILIZATION,
    SCAN_INTERVAL_SEC,
    ETFSnapshot,
    OpenTrade,
    PortfolioState,
    Position,
    SessionControls,
    SizingRejected,
    TradeLedger,
    authorize_order,
    check_can_enter,
    ndx_exposure,
    profile_for,
    rank_candidates,
    score_candidate,
    should_exit,
    should_rotate,
    size_trade,
)
from strategy.medik_etf_v2 import (
    MIN_EDGE_MULTIPLE,
    MIN_SCORE_V2,
    REENTRY_COOLDOWN_SEC_V2,
    V2_UNIVERSE,
    net_edge_check,
    qualifies_v2,
)
from strategy.medik_etf_ops import (
    WorkingOrder,
    verify_account_mode,
    kill_switch_active,
    kill_switch_reason,
    reconcile_startup,
)
from strategy.market_calendar import (
    close_minutes, coverage_warning, describe, is_trading_day,
)
from strategy.medik_mtf import OHLCV, drop_forming_bar

NY = ZoneInfo("America/New_York")

# The bot scans v2's trimmed, non-inverse universe (owner decision,
# 2026-08-26: "switch bot to v2 universe but keep commissions
# cost-efficient"). Reconciliation still recognises every symbol either
# version ever traded, so a legacy position in an inverse fund is adopted
# and managed at startup rather than declared incoherent.
SCAN_UNIVERSE = list(V2_UNIVERSE)
RECONCILE_UNIVERSE = list(dict.fromkeys([*V2_UNIVERSE, *ETF_UNIVERSE]))

LIVE_ENV_VAR = "MEDIK_ETF_LIVE"
RISK_ACK_ENV_VAR = "LIVE_RISK_ACK"
MODE_ENV_VAR = "MEDIK_ETF_MODE"
DRY_RUN_ENV_VAR = "MEDIK_ETF_DRY_RUN"
QUOTE_SOURCE_ENV_VAR = "MEDIK_ETF_QUOTE_SOURCE"      # "tws" | "cpapi"
CPAPI_URL_ENV_VAR = "MEDIK_ETF_CPAPI_URL"
ACCOUNT_ENV_VAR = "MEDIK_ETF_ACCOUNT"
PROTECTION_TIMEOUT_SEC = 10

# --------------------------------------------------------------- quotes
# reqTickers() asks IBKR for a SNAPSHOT, which is a different entitlement
# from the streaming subscription this account holds, so every US ETF came
# back as error 10089 ("does not extend support for API use") while TWS
# showed a live, fee-waived quote subscription. Verified 2026-08-24 with
# examples/mktdata_probe.py: streaming returns prices, snapshot does not.
#
# Streaming is not a drop-in replacement. A snapshot is one request that
# returns a value; a stream is a subscription that fills in over the next few
# seconds and then keeps updating, and each open one consumes a market-data
# line (TWS allows about 100). So subscribe once per symbol and read the same
# Ticker on later cycles: the wait becomes a startup cost paid once, not a
# per-scan cost paid every five minutes for twenty symbols.
LIVE_MARKET_DATA_TYPE = 1
QUOTE_WARMUP_SEC = 5.0
DELAYED_MARKET_DATA_TYPES = (3, 4)

# A streaming subscription that dies leaves its LAST values in the Ticker.
# Nothing errors, nothing goes NaN — the numbers simply stop moving, and a
# bot reading them cannot tell a quiet tape from a dead feed. Age is the only
# thing that distinguishes them, so it is checked explicitly.
#
# Generous on purpose: an illiquid inverse ETF can legitimately go a minute
# between prints without anything being wrong. This rejects a dead feed, not
# a slow one.
MAX_QUOTE_AGE_SEC = float(os.environ.get("MEDIK_ETF_MAX_QUOTE_AGE", 120))

# A market this wide is not a market worth crossing. At $286 a position is
# ~$70, so a 2% spread is $1.40 of instant cost against a $1.43 risk budget.
MAX_SPREAD_PCT = float(os.environ.get("MEDIK_ETF_MAX_SPREAD_PCT", 2.0))

# A last price far outside the current bid/ask is a stale or erroneous print.
MAX_LAST_DEVIATION_PCT = 5.0

# Holdings this strategy must LEAVE ALONE rather than adopt or refuse over.
# Empty by default on purpose: an exemption should be typed out deliberately.
# The QQQ buy-and-hold core position is the obvious candidate — but note that
# adding it does NOT make the bot able to trade QQQ-correlated ETFs, because
# the Nasdaq-beta cap still counts that exposure.
IGNORE_SYMBOLS: tuple = ()


LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

# How long to keep retrying TWS at startup. Task Scheduler fires at a fixed
# time; TWS restarts and re-logs-in on its own schedule, so on some mornings
# the bot arrives first. Two retries over six seconds gave up before TWS had
# finished starting. This waits instead — and still refuses to trade if the
# connection never comes.
CONNECT_WAIT_MINUTES = 20
CONNECT_RETRY_SECONDS = 30

EXIT_NO_TWS = 3          # distinct codes so Task Scheduler shows WHY it failed
EXIT_PREFLIGHT = 4
EXIT_INCOHERENT = 5


def log(msg: str) -> None:
    """Print, and append to a dated file.

    Unattended, print() alone is worthless: Task Scheduler discards stdout, so
    a morning that failed looks identical to one that never ran. Writing the
    file here rather than relying on shell redirection means the log exists
    however the process was launched.

    A logging failure must never stop a trading loop, so it is swallowed —
    stdout still carries the line for anyone watching.
    """
    line = f"[{datetime.now(NY):%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / f"medik_etf_{datetime.now(NY):%Y-%m-%d}.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def live_enabled() -> bool:
    """Exact-match opt-in. Never inferred, never truthy-ish."""
    return os.environ.get(LIVE_ENV_VAR, "") == "true"


def dry_run() -> bool:
    """Run the WHOLE pipeline against live quotes, but send nothing.

    This is the validation mode: real data, real scoring, real sizing, the
    real authorization checklist, and a logged decision — with placeOrder
    never called. It answers "would it have traded, and with what?" without
    risking a dollar.

    Exact-match, like the live gate. It fails toward NOT trading: anything
    other than the exact string "true" leaves dry-run off, but dry-run off
    still requires both live keys before an order can go out.
    """
    return os.environ.get(DRY_RUN_ENV_VAR, "") == "true"


def risk_acknowledged() -> bool:
    """Second key. Both must be turned for the bot to send an order.

    MEDIK_ETF_LIVE says "this program may trade". LIVE_RISK_ACK says "I have
    read what it is about to risk". They are separate because the first is
    easy to leave set in a shell profile or a scheduled task, and a single
    forgotten variable should not be the only thing between a scan and a
    live order.
    """
    return os.environ.get(RISK_ACK_ENV_VAR, "") == "true"


def arming_report() -> tuple[bool, list[str]]:
    """(armed, human-readable lines) describing exactly which keys are set.

    Also catches plausible-but-wrong variable names. Setting LIVE_TRADING
    instead of MEDIK_ETF_LIVE fails safe, but silently -- the operator would
    see a clean startup and assume it was armed.
    """
    live, ack = live_enabled(), risk_acknowledged()
    lines = [
        f"  {LIVE_ENV_VAR:<16} {'SET' if live else 'not set'}",
        f"  {RISK_ACK_ENV_VAR:<16} {'SET' if ack else 'not set'}",
    ]
    for wrong in ("LIVE_TRADING", "MEDIK_LIVE", "ETF_LIVE"):
        if os.environ.get(wrong):
            lines.append(f"  WARNING: {wrong} is set but is NOT a recognised "
                         f"variable — did you mean {LIVE_ENV_VAR}?")
    return (live and ack), lines


def _to_bars(raw) -> list[OHLCV]:
    return [OHLCV(b.open, b.high, b.low, b.close, float(b.volume or 0)) for b in raw]


def _now_minutes(now: datetime) -> int:
    return now.hour * 60 + now.minute


def _market_open(now: datetime) -> bool:
    """Open for trading right now, holidays and half-days included.

    Weekday-and-clock alone was fine while a human started the bot each
    morning -- nobody runs it on Thanksgiving. Under a scheduled task it
    fires every weekday whether the market exists that day or not.
    """
    if not is_trading_day(now.date()):
        return False
    return 9 * 60 + 30 <= _now_minutes(now) < close_minutes(now.date())


# ------------------------------------------------------------ data + scoring


def usable_price(x) -> float:
    """A positive price, or 0.0 when IBKR reported no value.

    IBKR uses NaN and -1 for "no value". `float(ticker.bid or 0)` does NOT
    handle that: NaN is truthy, so `NaN or 0` evaluates to NaN and the NaN
    propagates into every comparison downstream, where it is silently False.
    """
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v if v == v and v > 0 else 0.0        # v == v is False for NaN


def quote_age_seconds(ticker, now: datetime | None = None) -> float | None:
    """Seconds since this ticker last updated, or None if it never has.

    None is not zero. A ticker with no timestamp has produced no data at all,
    which is a stronger failure than an old one — the caller must not read it
    as "fresh".
    """
    stamp = getattr(ticker, "time", None)
    if stamp is None:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (now - stamp).total_seconds()
    except (AttributeError, TypeError):
        return None


def read_quote(ticker, fallback_close: float = 0.0, now: datetime | None = None,
               max_age_sec: float | None = None,
               max_spread_pct: float | None = None) -> tuple:
    """(bid, ask, last, problem). `problem` is "" when the quote is usable.

    Five ways a quote can be unusable, each refused explicitly:

    DELAYED   IBKR can serve 15-minute-old prices with no error at all. A
              5-minute strategy would keep trading, sizing stops off a quote
              three bars stale.
    MISSING   NaN or -1 in bid/ask/last.
    STALE     The numbers are present and simply stopped moving, because the
              feed died. Nothing errors; only the timestamp gives it away.
    CROSSED   bid above ask is a data glitch, not an arbitrage.
    WIDE      A spread this large is instant cost, not a market. At $286 of
              equity a 2% spread on a $70 position is $1.40 against a $1.43
              risk budget — the trade is lost before it starts.

    Failing to trade is recoverable. Trading on bad prices is not.
    """
    max_age_sec = MAX_QUOTE_AGE_SEC if max_age_sec is None else max_age_sec
    max_spread_pct = MAX_SPREAD_PCT if max_spread_pct is None else max_spread_pct

    md = getattr(ticker, "marketDataType", None) or LIVE_MARKET_DATA_TYPE
    if md in DELAYED_MARKET_DATA_TYPES:
        return 0.0, 0.0, 0.0, (
            f"DELAYED market data (marketDataType={md}) — refusing to trade a "
            "5-minute strategy on a 15-minute-old quote")

    bid, ask = usable_price(ticker.bid), usable_price(ticker.ask)
    last = (usable_price(ticker.last) or usable_price(ticker.close)
            or usable_price(fallback_close))
    if not (bid and ask and last):
        return 0.0, 0.0, 0.0, (
            f"no usable quote (bid={ticker.bid!r} ask={ticker.ask!r} "
            f"last={ticker.last!r} close={ticker.close!r})")

    age = quote_age_seconds(ticker, now)
    if age is None:
        return 0.0, 0.0, 0.0, "quote has no timestamp — cannot prove it is live"
    if age > max_age_sec:
        return 0.0, 0.0, 0.0, (
            f"STALE quote — last update {age:,.0f}s ago (limit {max_age_sec:,.0f}s); "
            "a dead feed keeps returning its last values without erroring")

    if ask < bid:
        return 0.0, 0.0, 0.0, f"crossed quote (bid {bid:,.2f} > ask {ask:,.2f})"

    spread_pct = (ask - bid) / ((ask + bid) / 2.0) * 100.0
    if spread_pct > max_spread_pct:
        return 0.0, 0.0, 0.0, (
            f"spread {spread_pct:.2f}% exceeds {max_spread_pct:.2f}% — "
            f"bid {bid:,.2f} ask {ask:,.2f}, not a tradeable market")

    mid = (bid + ask) / 2.0
    deviation = abs(last - mid) / mid * 100.0
    if deviation > MAX_LAST_DEVIATION_PCT:
        return 0.0, 0.0, 0.0, (
            f"last {last:,.2f} is {deviation:.1f}% away from the "
            f"{mid:,.2f} mid — stale or erroneous print")

    return bid, ask, last, ""


class QuoteFeed:
    """Streaming quotes, subscribed once per symbol and reused thereafter.

    Holding the subscriptions open is the point: the Ticker object updates in
    place, so every cycle after the first reads current prices with no wait.
    Twenty symbols is well inside the ~100-line TWS limit.
    """

    def __init__(self, ib, warmup_sec: float = QUOTE_WARMUP_SEC):
        self.ib = ib
        self.warmup_sec = warmup_sec
        self._tickers: dict = {}
        self._contracts: dict = {}

    def quote(self, contract):
        """The live Ticker for `contract`, subscribing on first use."""
        symbol = contract.symbol
        ticker = self._tickers.get(symbol)
        if ticker is None:
            ticker = self.ib.reqMktData(contract, "", False, False)
            self._tickers[symbol] = ticker
            self._contracts[symbol] = contract
            # Only the first read of a symbol waits; ticks keep arriving after.
            self.ib.sleep(self.warmup_sec)
        return ticker

    def cancel_all(self) -> int:
        """Release the market-data lines. Safe to call more than once."""
        released = 0
        for symbol, contract in list(self._contracts.items()):
            try:
                self.ib.cancelMktData(contract)
                released += 1
            except Exception as exc:
                log(f"  could not cancel market data for {symbol}: {exc!r}")
            self._tickers.pop(symbol, None)
            self._contracts.pop(symbol, None)
        return released


class ClientPortalFeed:
    """Quotes from the Client Portal Web API, shaped like QuoteFeed.

    Same `quote(contract)` signature as the TWS feed, and it returns an object
    read_quote() can consume, so freshness, spread, crossed-market and
    delayed-data checks all apply here identically. One set of rules for both
    providers rather than two that can drift.

    Contracts still come from TWS -- qualifyContracts() supplies the conid,
    which is what the Client Portal addresses instruments by.
    """

    def __init__(self, client: ClientPortalQuotes):
        self.client = client
        self._conids: dict = {}
        self._cache: dict = {}
        self._cache_stamp = 0.0

    def session_ok(self) -> tuple:
        return self.client.auth_status()

    def quote(self, contract):
        """Latest quote for `contract`, refreshing the whole batch at most
        once a second so a 20-symbol scan is one HTTP round trip, not twenty."""
        conid = int(getattr(contract, "conId", 0) or 0)
        if not conid:
            return None
        self._conids[contract.symbol] = conid
        if time.time() - self._cache_stamp > 1.0:
            try:
                self._cache = self.client.snapshot(list(self._conids.values()))
            except CpApiError as exc:
                log(f"  Client Portal snapshot failed: {exc}")
                self._cache = {}
            self._cache_stamp = time.time()
        return self._cache.get(conid)

    def cancel_all(self) -> int:
        released = len(self._conids)
        self.client.unsubscribe_all()
        self._conids.clear()
        self._cache.clear()
        return released


def make_quote_feed(ib):
    """(feed, description). Chooses the provider from the environment.

    Defaults to TWS. A provider is opt-in by exact name and an unrecognised
    value is refused rather than guessed, because silently falling back to a
    feed the operator did not choose is how a bot ends up trading data nobody
    audited.
    """
    source = os.environ.get(QUOTE_SOURCE_ENV_VAR, "tws").strip().lower()
    if source in ("", "tws"):
        return QuoteFeed(ib), "TWS socket API (reqMktData, streaming)"
    if source == "cpapi":
        url = os.environ.get(CPAPI_URL_ENV_VAR, "").strip()
        client = ClientPortalQuotes(url) if url else ClientPortalQuotes()
        return ClientPortalFeed(client), (
            f"IBKR Client Portal Web API ({client.base_url})")
    raise SystemExit(
        f"{QUOTE_SOURCE_ENV_VAR}={source!r} is not recognised — "
        "use 'tws' or 'cpapi'. Refusing to guess which feed to trade on.")


def build_snapshot(ib, symbol: str, market_is_open: bool,
                   feed: "QuoteFeed | None" = None) -> ETFSnapshot | None:
    qualified = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
    if not qualified:
        log(f"  {symbol:<5} could not qualify contract")
        return None
    contract = qualified[0]

    bars5 = _to_bars(ib.reqHistoricalData(
        contract, endDateTime="", durationStr="2 D", barSizeSetting="5 mins",
        whatToShow="TRADES", useRTH=True, formatDate=1))
    bars15 = _to_bars(ib.reqHistoricalData(
        contract, endDateTime="", durationStr="5 D", barSizeSetting="15 mins",
        whatToShow="TRADES", useRTH=True, formatDate=1))

    # Closed candles only — a forming bar makes scores flicker as it ticks.
    bars5 = drop_forming_bar(bars5, bar_complete=not market_is_open)
    bars15 = drop_forming_bar(bars15, bar_complete=not market_is_open)
    if len(bars5) < 25 or len(bars15) < 25:
        log(f"  {symbol:<5} insufficient bars (5m={len(bars5)} 15m={len(bars15)})")
        return None

    feed = feed if feed is not None else QuoteFeed(ib)
    ticker = feed.quote(contract)
    if ticker is None:
        log(f"  {symbol:<5} no quote returned by the feed")
        return None
    bid, ask, last, problem = read_quote(ticker, bars5[-1].close)
    if problem:
        log(f"  {symbol:<5} {problem}")
        return None

    # VWAP resets daily: pass only the CURRENT session's bars.
    session_bars = bars5[-78:] if len(bars5) > 78 else bars5
    dollar_volume = sum(b.close * b.volume for b in session_bars)

    return ETFSnapshot(
        symbol=symbol, price=last, bid=bid, ask=ask,
        bars_5m=session_bars, bars_15m=bars15,
        session_dollar_volume=dollar_volume,
    ), contract


# Statuses IBKR reports for an order that will never work again. "Inactive"
# is deliberately NOT here: IBKR also uses it for an order that is accepted
# but not yet working (an RTH order sent pre-open activates at the bell), so
# counting it as dead could let a second entry go out alongside a live one.
# Miscounting UP only stops trading; miscounting DOWN can double-position.
DEAD_ORDER_STATUSES = ("Filled", "Cancelled", "ApiCancelled")


def blocking_orders(ib, account: str = "") -> list:
    """Working orders that will veto a new entry, as (symbol, action, status).

    An order whose account IBKR left blank is counted as blocking. Blank is
    ambiguous, and the two ways of being wrong are not symmetric: over-counting
    only stops the bot trading, while under-counting can put a second entry out
    beside a live one.
    """
    return [(t.contract.symbol, t.order.action, t.orderStatus.status)
            for t in ib.openTrades()
            if t.orderStatus.status not in DEAD_ORDER_STATUSES
            and order_belongs_to(t, account)]


def resolve_account(managed) -> tuple[str, str]:
    """Pick the one account this run trades. ("", reason) means refuse.

    Thin wrapper over ibkr.accounts.resolve_account so this strategy's own
    variable takes precedence over the repo-wide one, and there is a single
    implementation of the rule rather than one per script.
    """
    return _resolve_account(managed, env_vars=(ACCOUNT_ENV_VAR, "IBKR_ACCOUNT"))


def subscribe_account(ib, account: str, timeout: float = 5.0) -> bool:
    """Explicitly subscribe to account updates, and confirm they arrived.

    THIS IS THE $0.00 BUG. reqAccountUpdates handles one account at a time, so
    ib_async only auto-subscribes when the login has exactly one managed
    account. With two, nothing is subscribed, accountValues() stays EMPTY, and
    every tag reads 0 — which presents as an empty account rather than as a
    missing subscription, so the bot sized against $0 buying power instead of
    reporting that it could not see the account at all.

    Returns False rather than raising: the caller turns that into a preflight
    FAIL, so a silent zero can never reach the risk engine.
    """
    if not account:
        return False
    ib.reqAccountUpdates(account)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(v.account == account and v.tag == "NetLiquidation" and v.value
               for v in ib.accountValues()):
            return True
        ib.sleep(0.25)
    return False


def read_portfolio(ib, account: str = "") -> PortfolioState:
    """Broker state for the risk engine, scoped to ONE account.

    Every read here is filtered on `account` in Python rather than by passing
    it to ib_async, so the scoping holds regardless of which of these calls the
    installed version accepts an account argument for. Filtering on the
    `.account` field each object already carries cannot silently stop working.

    Without that filter the tag dict is keyed by name alone, so two accounts
    collapse into whichever row arrived last — the bot would size against a
    balance belonging to a different account and never say so.

    market_value is the LIVE value and avg_cost is the per-share basis. They
    are separate fields because they answer different questions: exposure caps
    must measure what a position is worth NOW, while a restart needs the basis
    to rebuild the entry price. Deriving either from the other was wrong for
    whichever caller did not want it.
    """
    def mine(obj) -> bool:
        return belongs_to(obj, account)

    values = {r.tag: r.value for r in ib.accountValues()
              if mine(r) and r.currency in ("USD", "")}

    positions = tuple(
        Position(p.contract.symbol, p.position, float(p.marketValue),
                 float(p.averageCost))
        for p in ib.portfolio() if p.position and mine(p)
    )
    if not positions:
        # portfolio() is fed by the account-update subscription and is empty
        # until the first update lands; positions() is a direct query. It
        # carries no market value, so basis stands in.
        positions = tuple(
            Position(p.contract.symbol, p.position, p.position * float(p.avgCost),
                     float(p.avgCost))
            for p in ib.positions() if p.position and mine(p)
        )

    return PortfolioState(
        net_liquidation=float(values.get("NetLiquidation", 0) or 0),
        available_cash=float(values.get("AvailableFunds", 0) or 0),
        positions=positions,
        open_order_count=len(blocking_orders(ib, account)),
    )


# ------------------------------------------------------------------ execution


def verify_protection(ib, trade_legs) -> tuple[bool, str]:
    """A position is protected only when ALL three legs are acknowledged."""
    ib.sleep(PROTECTION_TIMEOUT_SEC)
    dead = ("Cancelled", "ApiCancelled", "Inactive")
    for leg, name in zip(trade_legs, ("parent", "stop", "target")):
        status = leg.orderStatus.status
        if status in dead:
            return False, f"{name} leg is {status}"
        if not status:
            return False, f"{name} leg has no status from IBKR"
    return True, ""


def emergency_shutdown(ib, armed: bool, open_trade, contracts, reason: str,
                       account: str = "") -> bool:
    """STOP_MEDIK sequence, in this exact order:

        disable entries -> cancel working orders -> flatten -> confirm flat

    Cancelling BEFORE flattening is the part that matters. A resting stop is
    a live SELL; if the flatten order goes in while the stop is still
    working, both can fill and the account ends up SHORT -- which this
    account cannot even hold. Cancel first, then close, then verify.
    """
    log("=" * 66)
    log(f"STOP_MEDIK DETECTED — {reason}" if reason else "STOP_MEDIK DETECTED")
    log("=" * 66)
    log("1. new entries disabled")

    log("2. cancelling working orders")
    cancelled = 0
    try:
        ib.reqAllOpenOrders()
        ib.sleep(1)
        for trade in ib.openTrades():
            if trade.orderStatus.status in DEAD_ORDER_STATUSES:
                continue
            if not order_belongs_to(trade, account):
                continue        # another account's order is not ours to cancel
            if armed:
                ib.cancelOrder(trade.order)
            cancelled += 1
            log(f"   cancel {trade.order.action} {trade.order.totalQuantity} "
                f"{trade.contract.symbol} {trade.order.orderType} "
                f"(id {trade.order.orderId})")
        if cancelled and armed:
            ib.sleep(3)
    except Exception as exc:
        log(f"   ERROR cancelling orders: {exc!r} — NOT flattening while orders "
            "may still be live; close manually in TWS")
        return False
    log(f"   {cancelled} order(s) handled")

    log("3. flattening open position")
    if open_trade is None:
        log("   no tracked position")
    elif not armed:
        log("   not armed — would flatten, sending nothing")
    else:
        flatten(ib, contracts.get(open_trade.symbol), open_trade.quantity,
                "STOP_MEDIK", account)

    log("4. confirming flat")
    try:
        ib.sleep(2)
        remaining = [(p.contract.symbol, p.position) for p in ib.positions()
                     if p.position and belongs_to(p, account)]
        if not remaining:
            log("   IBKR reports FLAT")
            flat = True
        else:
            log(f"   STILL HOLDING: {remaining}")
            log("   CHECK TWS MANUALLY — the process is exiting but the account is not flat")
            flat = False
    except Exception as exc:
        log(f"   could not verify positions: {exc!r} — CHECK TWS MANUALLY")
        flat = False

    log("5. exiting")
    return flat


def flatten(ib, contract, quantity: int, reason: str, account: str = "") -> None:
    """Emergency exit: close an unprotected position with a marketable order."""
    log(f"  EMERGENCY FLATTEN {contract.symbol} x{quantity} — {reason}")
    order = MarketOrder("SELL", quantity)
    order.tif = "DAY"
    if account:
        order.account = account
    ib.placeOrder(contract, order)
    ib.sleep(5)
    log(f"  flatten status: {order.orderId} -> "
        f"{ib.trades()[-1].orderStatus.status if ib.trades() else 'unknown'}")


def place_bracket(ib, contract, sized, controls: SessionControls,
                  account: str = "") -> bool:
    """Submit entry+stop+target, verify all three, or flatten. Returns success."""
    bracket = ib.bracketOrder(
        "BUY", sized.quantity,
        limitPrice=round(sized.entry, 2),
        takeProfitPrice=round(sized.target, 2),
        stopLossPrice=round(sized.stop, 2),
    )
    # Entry dies at the close (a stale limit is wrong against the next open);
    # protective legs are GTC so they survive as long as the position does.
    bracket[0].tif = "DAY"
    for leg in bracket[1:]:
        leg.tif = "GTC"
    # With more than one account on the login, an untagged order is not
    # merely ambiguous — TWS may route it to the wrong account or reject
    # it. Name the account on every leg.
    if account:
        for leg in bracket:
            leg.account = account

    legs = [ib.placeOrder(contract, o) for o in bracket]
    log(f"  submitted: parent={bracket[0].orderId} "
        f"stop={bracket[2].orderId} target={bracket[1].orderId}")

    protected, why = verify_protection(ib, legs)
    parent_filled = legs[0].orderStatus.filled or 0

    if protected:
        for leg, name in zip(legs, ("parent", "stop", "target")):
            log(f"    {name:<7} {leg.orderStatus.status:<12} "
                f"filled={leg.orderStatus.filled}")
        return True

    log(f"  BRACKET FAILURE: {why}")
    if parent_filled > 0:
        flatten(ib, contract, int(parent_filled), why, account)
    else:
        for o in bracket:
            ib.cancelOrder(o)
        log("  parent unfilled — all legs cancelled")
    controls.disable(f"bracket failure: {why}")
    return False


# ----------------------------------------------------------------------- main


def scan_once(ib, armed: bool, controls: SessionControls, ledger: TradeLedger,
              open_trade: OpenTrade | None,
              contract_sink: dict | None = None,
              account: str = "",
              feed: "QuoteFeed | None" = None) -> OpenTrade | None:
    """One scan cycle. Returns the open trade after this cycle, or None.

    Order of business: read account -> manage any open position -> if flat,
    score the universe, rank it, size the best, run the authorisation
    checklist, and submit automatically if every check passes.
    """
    now = datetime.now(NY)
    market_is_open = _market_open(now)
    now_min = _now_minutes(now)
    now_ts = time.time()

    state = read_portfolio(ib, account)
    if state.net_liquidation <= 0:
        log("account data unavailable this cycle — NO ORDER")
        return open_trade

    exposure = ndx_exposure(state.positions)
    log(f"equity ${state.net_liquidation:,.2f}  buying power ${state.available_cash:,.2f}  "
        f"NDX-beta exposure ${exposure:,.2f} ({exposure / state.net_liquidation:.0%})")
    log(f"positions: {[(p.symbol, round(p.market_value, 2)) for p in state.positions] or 'none'}")

    # ---- score the universe first: needed to manage the position as well as
    # to find a new one.
    scores, contracts = {}, {}
    for symbol in SCAN_UNIVERSE:
        built = build_snapshot(ib, symbol, market_is_open, feed)
        if built is None:
            continue
        snap, contract = built
        contracts[symbol] = contract
        cs = score_candidate(snap)
        scores[symbol] = cs

    if contract_sink is not None:
        contract_sink.update(contracts)   # so a kill switch can flatten

    ranked = rank_candidates(list(scores.values()))
    top = ranked[:5]
    if top:
        log("top candidates: " + ", ".join(
            f"{c.symbol}={c.score:.0f}" for c in top))
    for c in sorted(scores.values(), key=lambda x: -x.score):
        detail = ", ".join(c.reasons) if c.signal == "TRADE" else "; ".join(c.rejections)
        log(f"DATA | {c.symbol:<5} | price={c.price:>8.2f} | rvol={c.rvol:>5.2f} | "
            f"rsi={c.rsi:>5.1f} | 15m={c.trend_15m:<7} | score={c.score:>5.1f} | "
            f"{c.signal:<7} | {detail}")
    for i, c in enumerate(ranked[:3], 1):
        log(f"RANK | #{i} {c.symbol} | score={c.score:.1f}")

    # ---- manage an existing position
    if open_trade is not None:
        current = scores.get(open_trade.symbol)
        price = current.price if current else open_trade.entry
        exiting, why = should_exit(open_trade, current, price, now_min)
        if exiting:
            log(f"EXIT {open_trade.symbol}: {why}")
            if armed:
                flatten(ib, contracts.get(open_trade.symbol), open_trade.quantity,
                        why, account)
                controls.trades_completed += 1
            return None
        if ranked:
            rotate, rwhy = should_rotate(open_trade, current, ranked[0], price)
            log(f"rotation: {'YES — ' + rwhy if rotate else 'no — ' + rwhy}")
            if rotate and armed:
                flatten(ib, contracts.get(open_trade.symbol), open_trade.quantity,
                        rwhy, account)
                controls.trades_completed += 1
                return None
        log(f"holding {open_trade.symbol} x{open_trade.quantity} "
            f"(stop ${open_trade.stop:.2f} target ${open_trade.target:.2f})")
        return open_trade

    # ---- look for a new entry
    if not ranked:
        log("NO TRADE | reason=no qualifying setup among "
            f"{len(scores)} scored symbols")
        return None

    best = ranked[0]

    # v2 gate 1: stricter technical qualification (score floor 85 and a
    # pullback/reclaim required) on top of v1's TRADE signal.
    ok_v2, why_v2 = qualifies_v2(best)
    if not ok_v2:
        log(f"NO TRADE | reason=v2 filter | {best.symbol} | {why_v2}")
        return None

    try:
        sized = size_trade(best, state)
    except SizingRejected as exc:
        log(f"NO TRADE | reason=sizing | {best.symbol} | {exc}")
        return None

    # v2 gate 2 -- the cost rule: the target must clear the full round trip
    # (both commissions, spread crossed twice, slippage) by MIN_EDGE_MULTIPLE
    # or the trade is refused regardless of how good the chart looks.
    edge = net_edge_check(sized.symbol, sized.quantity, sized.entry,
                          sized.stop, sized.target)
    log(f"EDGE | {sized.symbol} | {edge.reason}")
    if not edge.passes:
        log(f"NO TRADE | reason=net edge vs cost | {sized.symbol}")
        return None

    log(f"SIGNAL | BUY {sized.symbol} | score={best.score:.1f} | "
        f"{', '.join(best.reasons)}")
    log(f"SELECTED {sized.symbol} score {best.score:.1f} "
        f"leverage {profile_for(sized.symbol).leverage}x — {', '.join(best.reasons)}")
    log(f"  allocation ${sized.notional:,.2f} "
        f"({sized.notional / state.available_cash:.0%} of buying power, "
        f"limit {MAX_CAPITAL_UTILIZATION:.0%})  qty {sized.quantity} whole shares")
    log(f"  entry ${sized.entry:,.2f}  stop ${sized.stop:,.2f}  "
        f"target ${sized.target:,.2f}  R:R {sized.reward_risk:.1f}:1")
    log(f"  risk ${sized.risk_dollars:,.2f} "
        f"({sized.risk_dollars / state.net_liquidation:.2%} of equity)  "
        f"binding cap: {sized.binding_cap}")

    auth = authorize_order(
        live_enabled=armed, connected=ib.isConnected(), state=state,
        controls=controls, candidate=best, sized=sized,
        now_minutes=now_min, ledger=ledger, now_ts=now_ts,
        close_minutes=close_minutes(now.date()),
    )
    if not auth:
        log(f"RISK | FAIL | {sized.symbol} | failed={', '.join(auth.failures)}")
        log(f"NO TRADE | reason=risk checks | {', '.join(auth.failures)}")
        return None

    log(f"RISK | PASS | {sized.symbol} | qty={sized.quantity} | "
        f"entry=${sized.entry:,.2f} | stop=${sized.stop:,.2f} | "
        f"target=${sized.target:,.2f} | max_loss=${sized.risk_dollars:,.2f}")

    # DRY RUN stops here, AFTER every check has run and been logged. Placing
    # it later would mean the decision was never really made; placing it
    # earlier would mean the checks were never really tested.
    if dry_run():
        log(f"DRY RUN | would submit BUY {sized.quantity} {sized.symbol} "
            f"@ ${sized.entry:,.2f} — NO ORDER SENT")
        return None

    # Authorised by the checklist alone. No interactive confirmation.
    ledger.mark_pending(sized.symbol)
    ok = place_bracket(ib, contracts[sized.symbol], sized, controls, account)
    if not ok:
        ledger.mark_failed(sized.symbol)
        log(f"ENTRIES DISABLED: {controls.disabled_reason}")
        return None

    ledger.mark_entered(sized.symbol, now_ts)
    controls.trades_completed += 1
    return OpenTrade(sized.symbol, sized.quantity, sized.entry, sized.stop,
                     sized.target, now_ts)


def connect_with_wait(client, deadline_minutes: int = CONNECT_WAIT_MINUTES):
    """Connect to TWS, retrying until a deadline. None if it never came up.

    Returns rather than raises so the caller can exit with a clear message
    instead of a traceback. NOTHING about the trading path is relaxed by
    waiting: without a connection there is no account, no quote and no order,
    and authorize_order()'s ibkr_connected check fails independently.
    """
    deadline = time.time() + deadline_minutes * 60
    # A second, independent bound. The deadline assumes the clock advances
    # between attempts, which is true only because we sleep — so a very short
    # or suppressed sleep would spin forever, filling the log. Bounding the
    # attempt count as well means neither condition alone has to hold.
    max_attempts = max(1, int(deadline_minutes * 60 / max(CONNECT_RETRY_SECONDS, 1)) + 2)
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        try:
            ib = client.connect(timeout=15, retries=0)
            if ib.isConnected():
                log(f"IBKR: CONNECTED on attempt {attempt}")
                return ib
            log(f"IBKR: attempt {attempt} returned a disconnected client")
        except Exception as exc:
            log(f"IBKR: attempt {attempt} failed — {type(exc).__name__}: {exc}")

        remaining = deadline - time.time()
        if remaining <= 0:
            log(f"IBKR: no connection after {deadline_minutes} minutes "
                f"({attempt} attempts)")
            return None
        log(f"  retrying in {CONNECT_RETRY_SECONDS}s "
            f"({remaining / 60:.0f} min left before giving up)")
        time.sleep(min(CONNECT_RETRY_SECONDS, remaining))

    log(f"IBKR: giving up after {attempt} attempts")
    return None


def main() -> int:
    armed, arming_lines = arming_report()
    log("=" * 66)
    log("MEDIK ETF ACTIVE LIVE")
    log("=" * 66)

    # Checked before connecting: if the operator stopped the bot, that
    # decision outranks everything below it.
    if kill_switch_active():
        log(f"STOP_MEDIK present — not starting. {kill_switch_reason()}")
        return 0

    client = IBKRClient()
    ib = connect_with_wait(client)
    if ib is None:
        log("EXITING — no TWS connection, so no account, no market data and "
            "NO ORDER. Check that TWS is open, logged in, and that "
            "'Enable ActiveX and Socket Clients' is on with port "
            f"{client.port}.")
        return EXIT_NO_TWS
    controls = None
    ledger = TradeLedger(cooldown_sec=REENTRY_COOLDOWN_SEC_V2)
    open_trade: OpenTrade | None = None
    last_contracts: dict = {}

    # Requested explicitly rather than left at the library default, so the log
    # records which data the run was authorised for. IBKR can serve delayed
    # data silently; read_quote() refuses it, and this line says what was asked.
    ib.reqMarketDataType(LIVE_MARKET_DATA_TYPE)
    feed, feed_desc = make_quote_feed(ib)

    try:
        accounts = ib.managedAccounts()
        account, account_msg = resolve_account(accounts)
        log(f"MANAGED ACCOUNTS: {', '.join(accounts) if accounts else 'UNAVAILABLE'}")
        log(f"ACCOUNT: {account_msg}")

        # Subscribing is what makes accountValues() non-empty on a login with
        # more than one account. Do it BEFORE the first read, and report the
        # outcome, so an unsubscribed account can never be read as $0.00.
        subscribed = subscribe_account(ib, account) if account else False
        state = read_portfolio(ib, account)
        log(f"NET LIQUIDATION: ${state.net_liquidation:,.2f}")
        log(f"AVAILABLE FUNDS: ${state.available_cash:,.2f}")

        # Preflight is a single explicit verdict, so a failed start can never
        # be mistaken for a quiet one.
        mode = os.environ.get(MODE_ENV_VAR, "")
        mode_ok, mode_msg = verify_account_mode(mode, [account] if account else [],
                                               client.port)
        failures = []
        if not ib.isConnected():
            failures.append("not connected to IBKR")
        if not accounts:
            failures.append("no managed accounts")
        if not account:
            failures.append(account_msg)
        elif not subscribed:
            failures.append(
                f"no account update for {account} within the timeout — balances "
                "would read $0.00; refusing to trade against unknown equity")
        if state.net_liquidation <= 0:
            failures.append("net liquidation unavailable or zero")
        if not mode_ok:
            failures.append(mode_msg)

        log(f"MODE: {mode_msg}")
        log(f"ACCOUNT PREFLIGHT: {'PASS' if not failures else 'FAIL'}")
        if failures:
            for f in failures:
                log(f"  FAIL | {f}")
            log(f"  set {ACCOUNT_ENV_VAR}=<account id> to choose the account, and "
                f"{MODE_ENV_VAR}=paper or {MODE_ENV_VAR}=live to match it")
            log("EXITING — no scan, no orders")
            return EXIT_PREFLIGHT
        if mode.strip().lower() == "paper":
            log("PAPER MODE — fills come from IBKR's paper account, not from "
                "this program; the live gate still applies")

        controls = SessionControls(equity_start_of_session=state.net_liquidation)

        # RECONCILE. A restart loses the in-memory position, so rebuild it
        # from the broker's own working orders. Without this the bot would
        # believe it is flat and silently stop managing a live trade.
        ib.reqAllOpenOrders()
        ib.sleep(1)
        working = [
            WorkingOrder(t.contract.symbol, t.order.action, t.order.orderType,
                         float(t.order.totalQuantity or 0),
                         float(t.order.lmtPrice or t.order.auxPrice or 0))
            for t in ib.openTrades()
            if t.orderStatus.status not in DEAD_ORDER_STATUSES
            and order_belongs_to(t, account)
        ]
        decision = reconcile_startup(list(state.positions), working, RECONCILE_UNIVERSE,
                                     ignore_symbols=IGNORE_SYMBOLS)
        for note in decision.notes:
            log(f"RECONCILIATION | {note}")
        log(f"RECONCILIATION | decision = {decision.action}")

        if not decision.may_run:
            log("ERROR | account state is incoherent — not starting")
            return EXIT_INCOHERENT

        open_trade = decision.adopted
        if open_trade is not None:
            log(f"RECONCILIATION | resuming management of {open_trade.symbol} "
                f"x{open_trade.quantity} stop ${open_trade.stop:,.2f} "
                f"target ${open_trade.target:,.2f}")

        for line in arming_lines:
            log(line)
        if dry_run():
            log("AUTOMATIC TRADING: DRY RUN — full pipeline on live quotes, "
                "NO ORDERS WILL BE SENT")
        else:
            log(f"AUTOMATIC TRADING: {'ENABLED' if armed else 'DISABLED'}")
        log(f"CAPITAL UTILIZATION: UP TO {MAX_CAPITAL_UTILIZATION:.0%}")
        log(f"STRATEGY: v2 — {len(SCAN_UNIVERSE)} non-inverse ETFs, "
            f"score floor {MIN_SCORE_V2:.0f}, reclaim required, "
            f"net edge >= {MIN_EDGE_MULTIPLE:.1f}x round-trip cost, "
            f"re-entry cooldown {REENTRY_COOLDOWN_SEC_V2 // 60} min")
        log(f"MARKET DATA: {feed_desc}")

        # A quote provider that cannot prove it is authenticated must not be
        # traded on. Checked at startup so the run fails loudly at 06:45
        # rather than silently skipping every symbol for six hours.
        if hasattr(feed, "session_ok"):
            ok_session, why_session = feed.session_ok()
            log(f"QUOTE SESSION: {'OK — ' if ok_session else 'FAILED — '}{why_session}")
            if not ok_session:
                log("EXITING — the quote feed is not usable, so there is no "
                    "price to trade on and NO ORDER can be justified")
                return EXIT_PREFLIGHT
        log(f"SCAN INTERVAL: {SCAN_INTERVAL_SEC // 60} MINUTES")
        today = datetime.now(NY).date()
        log(f"SESSION: {describe(today)}")
        stale = coverage_warning(today)
        if stale:
            log(f"WARNING | {stale}")
        if not armed:
            log(f"LIVE ETF TRADING DISABLED — both {LIVE_ENV_VAR}=true and "
                f"{RISK_ACK_ENV_VAR}=true are required. Scanning only.")
        log("=" * 66)

        if not decision.may_trade:
            controls.disable(
                "unmanaged position: " + ", ".join(decision.unmanaged))
            log(f"ERROR | NEW ENTRIES BLOCKED — {controls.disabled_reason}")
            log("ERROR | the loop will run, report and honour STOP_MEDIK, but will "
                "not open a position until this is resolved")

        while True:
            now = datetime.now(NY)

            # Checked FIRST, before market hours and before any scan, so the
            # switch works whether or not the session is open.
            if kill_switch_active():
                controls.disable("STOP_MEDIK")
                emergency_shutdown(ib, armed, open_trade, last_contracts,
                                   kill_switch_reason(), account)
                break

            if not _market_open(now):
                log(describe(now.date()))
                if open_trade is not None:
                    log("market closed with a position still open — "
                        "protective legs are GTC and remain working")
                log(f"market closed at {now:%H:%M} ET — exiting loop")
                break

            log("-" * 66)
            log(f"scan @ {now:%H:%M:%S} ET")
            try:
                open_trade = scan_once(ib, armed, controls, ledger, open_trade,
                                       last_contracts, account, feed)
            except Exception as exc:  # one bad cycle must not kill the run
                controls.execution_errors += 1
                log(f"CYCLE ERROR ({controls.execution_errors}): {exc!r}")
                if controls.execution_errors >= 3:
                    controls.disable(f"repeated execution errors: {exc!r}")
                    log(f"ENTRIES DISABLED: {controls.disabled_reason}")

            if (controls.entries_disabled and open_trade is None
                    and not controls.disabled_reason.startswith("unmanaged position")):
                log(f"ERROR | entries disabled ({controls.disabled_reason}) and "
                    "flat — exiting")
                break

            # Sleep in slices so the kill switch is honoured within ~10s
            # rather than after a full scan interval.
            for _ in range(max(1, SCAN_INTERVAL_SEC // 10)):
                if kill_switch_active():
                    break
                ib.sleep(10)
    finally:
        released = feed.cancel_all()
        if released:
            log(f"released {released} market-data subscription(s)")
        client.disconnect()
        log("disconnected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
