# Medik — IBKR trading automation

## Standing risk rules (set by the account owner, 2026-07-21)

These apply to every trade idea, order draft, or position plan produced in
this repo or via connected IBKR tools. They are enforced in code by
`strategy/config.py` + `strategy/risk.py` — do not bypass them.

1. **Max 20% of available funds per trade.** Never size a single position
   above 20% of the account's current available funds (not net liquidation).
1a. **EXCEPTION — approved index ETFs may go to 70% of available funds**
   (owner decision, 2026-08-05, adopting QQQ buy-and-hold as the account's
   core strategy). Eligibility is by **explicit whitelist**
   (`strategy/core_holdings.CORE_ETFS`), never by asset class: the owner said
   "exempt ETFs", but TQQQ/SQQQ/QLD/QID are ETFs too, and a 3x daily-reset
   fund is not a milder QQQ — it decays in chop and is structurally unfit for
   buy-and-hold. The 20% cap exists to limit *single-issuer* blowup risk,
   which is why relaxing it for a 100-company index fund is coherent and
   relaxing it for a leveraged derivative would invert its purpose. Adding a
   symbol to the whitelist is a deliberate act, not an inference.
   Enforced by `strategy.risk.size_core_holding`.
1b. **Max 80% of the account deployed in total** (rule updated by the owner,
   2026-07-23). Positions may stack up to 80% of equity combined; at least
   20% of equity always stays in cash. Enforced via
   `strategy.risk.portfolio_headroom`.
2. **Minimum risk/reward** — 1:3 for the gate strategy (target derived from
   stop distance) or 1:2 for the pullback strategy, where the target is the
   *actual* recent swing-high resistance rather than a derived multiple
   (adopted 2026-08-04, see rule 8). Either way the target must clear its
   required R:R or the trade doesn't fire — `strategy.risk.size_position`
   enforces this whether the target is derived or explicitly given.
3. **Multi-indicator confirmation required (gate strategy).** A trade
   signal only counts if ALL of these agree on the same direction (see
   `strategy/signals.py`): EMA50/EMA200 trend, RSI(14) in the directional
   band, MACD(12,26,9) alignment, and volume >= its 20-day average.
4. **Score >= 70/100 to be tradeable (gate strategy only).** Every
   gate-passing candidate is ranked 0-100 by `strategy/scoring.py` (trend
   strength, momentum quality, volume surge, risk quality). Recalibrated
   from 90 on 2026-08-04 — backtested against 506 gate-passing bars across
   85 symbols over ~1 year, the max score ever achieved was 82.3, so 90 was
   unreachable and silently meant the system would never trade at all.
5. **Max 1% of net liquidation at risk per trade** (tightened from 2% on
   2026-08-04 — conventional swing-trading guidance for a small/developing
   account; see the provenance note in rule 8. 2% remains the hard ceiling
   this must never be raised above without the owner), in addition to the
   20% notional cap — whichever is tighter binds. Enforced in
   `strategy.risk.size_position` via its `net_liquidation` argument.
6. **Portfolio circuit breakers** (`strategy/risk_limits.py`): new entries
   halt automatically if the account hits its daily loss limit (3%), weekly
   drawdown limit (6%), monthly drawdown limit (10%), max concurrent
   positions (5), or the 80% exposure cap — checked before every scan via
   `check_trade_allowed`.
7. **Only completed session bars feed the gate.** `strategy/data_quality.py`
   drops any trailing daily bar whose US regular session hasn't closed yet
   (America/New_York wall-clock, not a UTC-hour proxy) — added after a
   stale mid-session bar produced a false gate failure on 2026-08-03.
8. **Second long-signal source: trend-pullback strategy** (`strategy/pullback.py`,
   added 2026-08-04). 200-SMA rising, pullback to the 8-EMA holding above the
   200-SMA, reclaim with a higher low. Stop below the pullback low; target
   the recent swing high (real resistance) at minimum 1:2, per rule 2 —
   evaluated independently of the gate strategy every scan, long-only. Both
   strategies' decisions are labeled `strategy=gate` / `strategy=pullback`
   in the journal so performance can be compared per-strategy.
   **PROVENANCE — read this before citing it.** The owner asked for this to
   be modeled on Humbled Trader (Shay Huang, humbledtrader.com). That site
   is **blocked from this environment (HTTP 403)** and was never read. These
   rules come from Claude's general knowledge of trend-pullback swing
   trading and are only *attributed* to that methodology — they have NOT
   been checked against her actual published material. Do not describe them
   as "her" rules or as verified from source. The same caveat applies to the
   1%-risk and 1:2-R:R choices in rules 2 and 5: sound, conventional swing
   practice, but not sourced.
   **VALIDATION STATUS: TESTED — DOES NOT HAVE A TRADEABLE EDGE.**
   See "Backtest verdict" below. Do not present pullback signals as
   validated; they are not.

## Backtest verdict (2026-08-05) — READ BEFORE PROPOSING ANY LIVE TRADE

Both strategies were backtested over **2 years of real IBKR daily bars,
201 symbols**, long-only, with the full risk engine and fractional sizing
active (now `backtest/run_2y_backtest.py`, `backtest/net_of_commission.py` —
both read `data/data2y`; see `docs/backtest-verdict.md` to reproduce).

| | gross P&L | commissions | **net** |
|---|---|---|---|
| pullback (149 trades) | +$4.72 | −$112.47 | **−$107.75 (−35.9%)** |
| gate (45 trades) | +$6.73 | −$31.65 | **−$24.92 (−8.3%)** |

Two separate problems, and the second is the fatal one:

1. **Commissions.** This account's real schedule (measured from 90 days of
   actual fills) is `clamp($0.005/share, min $1.00, max 1% of trade value)`.
   At ~$35 positions the 1% cap binds both ways, so **every round trip costs
   ~2% of position value**. Average gross profit per pullback trade was
   ~$0.03 against ~$0.75 of commission.
2. **There is essentially no edge to begin with.** Gross profit factor was
   **1.03** (pullback) — +1.57% over *two years*, before costs. Scaling the
   account does not rescue it: gross scales linearly but commissions floor
   at $1.00/fill (~$298 for 298 fills), so **break-even is around a
   $19,000 account, at which point the strategy earns roughly $0** while
   an index fund returned far more over the same window.

### Follow-up research (2026-08-05): what actually beat it

After the above, standard low-frequency strategies were tested on 5 years
of real IBKR ETF bars, net of the same commission model
(`backtest/lowfreq.py`, `backtest/run_lowfreq_comparison.py`):

| strategy | total | CAGR | maxDD | fills | commission |
|---|---|---|---|---|---|
| **QQQ buy & hold** | **+121.8%** | **17.3%** | 22.9% | **1** | **$1.00** |
| QQQ 200-day timing (monthly) | +90.1% | 13.7% | **13.6%** | 33 | $6.35 |
| dual momentum (quarterly) | +84.7% | 13.1% | 23.3% | 19 | $11.36 |
| SPY buy & hold | +84.0% | 13.0% | 19.0% | 1 | $1.00 |
| SPY 200-day timing | +37.6% | 6.6% | 11.5% | 32 | $9.67 |
| x-sectional stock momentum (best of 4) | **−15.9%** | −3.4% | 43.5% | 59 | $23.24 |

**Buy-and-hold beat every active strategy tested.** Cross-sectional stock
momentum — the closest analogue to what this repo was originally built to
do — lost money in all four configurations. The only thing timing bought
was drawdown (13.6% vs 22.9%), at a cost of ~3.6%/yr in return.

Caveats, stated so nobody over-reads the table: the window starts Aug 2022,
i.e. *after* the 2022 bear (QQQ −33.7%, 35.6% drawdown), which flatters
buy-and-hold and under-tests timing's whole purpose. Re-running from
May 2022 did not change the ordering. These are also unadjusted price bars,
so dividends are missing — which *understates* buy-and-hold, since it holds
continuously while timing sits in cash. Five years is one regime, mostly a
tech bull market; this is evidence, not proof.

**Therefore: do not recommend, draft, or place live trades from these
strategies as configured.** If the owner asks to trade them anyway, say
plainly that the tested expectancy is negative and why. Legitimate paths
forward are (a) find a genuinely higher-expectancy setup and re-test it the
same way, (b) trade far less frequently so commission drag matters less, or
(c) accept that a ~$300 account cannot overcome a 2%-per-round-trip cost
structure and treat this repo as research/paper-trading infrastructure.
Any future strategy change MUST be re-run through
`net_of_commission.py` before it is described as working.

## ADOPTED STRATEGY (2026-08-05): QQQ buy-and-hold

Following the research above, the owner chose **QQQ buy-and-hold** as the
account's core strategy, exempted index ETFs from the 20% per-trade cap, and
set the ETF cap at **70% of available funds** (rules 1a above).

**This strategy is "do nothing", and that is the point.** It won the
comparison precisely because it does not trade: 1 fill, $1.00 of commission,
+17.3% CAGR, versus every active overlay tested. The failure mode here is not
missing a signal — it is *intervening*. Do not propose rotations, overlays,
timing filters, or "improvements" to it absent new evidence run through
`net_of_commission.py`.

Mechanics:

- Sizing goes through `strategy.risk.size_core_holding`, a **separate path**
  from `size_position` — not a flag on it. Buy-and-hold has no stop and no
  target, and `size_position` derives both the target and the size *from the
  stop distance*, so it structurally cannot express this position.
- **Rule 5 (max 1% of net liquidation at risk) is INAPPLICABLE here, not
  waived.** "Capital at risk" in that rule means "dollars lost if the stop
  fills". With no stop, that quantity is undefined — it is not zero, and not
  the position value either. `CoreHoldingPlan` therefore has no such field;
  don't invent one. Risk is controlled instead by diversification (100
  companies), the 70% cap, and the 20% cash floor.
- The **80% deployed cap (rule 1b) still binds** and is enforced inline by
  `size_core_holding`; whichever of the two caps is tighter wins.
- The **drawdown circuit breakers are deliberately NOT applied** to core
  holdings. They exist to stop a swing trader revenge-trading a losing
  streak; applied to a decades-horizon index entry they would forbid buying
  *during a dip* and create a loop where a falling market permanently blocks
  establishing the position.
- Monitoring is `examples/check_core_holdings.py` — a **report, not a trading
  loop**, with no sell trigger. A drawdown line in it is never a sell signal:
  QQQ fell 22.9% inside the window that returned +121.8%.
- Execution policy is unchanged — Claude drafts, the owner submits.

### CORE POSITION ESTABLISHED (2026-08-07)

The QQQ core holding is **open**. Filled 08:27 PT / 11:27 ET:

    BUY 0.2836 QQQ @ $722.74   commission $1.00   net $204.97

Cost basis $726.27/sh including commission. At entry: 69.9% of available
funds (ETF cap 70%), 70.2% of equity deployed (portfolio cap 80%), $87.21
cash, $292.17 net liquidation. Both legacy positions were closed the same
morning to fund it: F 9 @ $13.79 (−$6.91 realized), JPM 0.09 @ $355.64
(+$0.02). Total commissions for the day $2.33.

**This position is now in "do nothing" mode.** No stop, no target, no
sell trigger. Monitoring is `examples/check_core_holdings.py`, a report.

### WHEN TO SELL THE CORE HOLDING (policy, 2026-08-07)

Asked directly by the owner. Buy-and-hold has **no sell rule derived from
price**; it has sell reasons derived from the owner's life. There are
exactly three, and none of them is a number on a screen:

1. **The money is needed** — a real expense arrives. Sell at whatever
   price exists that day. This is the ordinary case and requires no
   analysis.
2. **The holding stops being what was bought** — the fund is liquidated or
   restructured, or the index methodology changes such that it is no
   longer a broad, unleveraged, diversified fund. A 30% decline is *not* a
   thesis break; it is what the asset does.
3. **A structurally better fund is chosen** — e.g. moving to VOO/VTI for
   broader coverage than the tech-heavy Nasdaq-100. A calm
   portfolio-construction decision made once, not a timing call. Both are
   already whitelisted in `core_holdings.CORE_ETFS`.

**Explicitly NOT reasons to sell:** it fell; it rose a lot; it "looks
toppy"; a scary headline; locking in gains; hitting a round percentage; a
drawdown line in `check_core_holdings.py`; anyone forecasting a crash.

**Also refuse to attach sell orders to this position** — no take-profit
limit, no protective stop. A +10% take-profit inside the tested window
would have exited within months and forgone the remaining ~112 percentage
points; any meaningful stop would have sold at the bottom of the 22.9%
drawdown the position fully recovered from. Both convert buy-and-hold into
timing, which lost in every configuration tested. GTC orders also linger
and **cannot be cancelled from Claude's side** (no cancel/modify tool on
the connector), so a forgotten sell order is a durable hazard.

**State the downside honestly whenever the +121.8% figure is cited.** That
came from a five-year, mostly tech-bull window. The Nasdaq-100 fell **83%**
from 2000-2002 and took roughly fifteen years to regain its nominal peak.
Nothing tested rules out a repeat. The conclusion is *not* to add a stop —
it is that this position should only hold money with a genuinely long
horizon (**if it is needed within ~3 years it should not be in QQQ at
all**, but in cash or a GIC), and that broadening beyond one tech-heavy
index is the right diversification move as the account grows.

**"I bought high" is not a reason to act.** The owner said this minutes
after the fill (QQQ was +1.14% on the day, filled 6c off the day high).
The honest arithmetic: the entry was $5.78 above the session low, which on
0.2836 shares is **$1.64**. Selling to re-buy costs $2.00 in commissions
alone — *fixing it costs more than the error*, before considering that the
re-entry might be higher still. Also worth saying: 52-week range is
$555.60–$748.65, so the fill was 3.5% below the 52-week high; "day high"
is not "high". And entry timing is specifically the thing the research
found doesn't pay — the 200-day timing overlay, a disciplined attempt to
buy lower, cost 3.6%/yr versus buy-and-hold. Answer this the same way if
it recurs: quantify the gap, quantify the cost of correcting it, and say
plainly that intervening is the failure mode of this strategy.

### Order-routing reality (learned across 2026-08-05 → 08-07)

- **`create_order_instruction` has never produced a fill.** Across two
  sessions and many drafts, every instruction sat at `is_new: true` and
  none reached the market; *every* order that actually executed was typed
  into TWS by the owner. Treat the instruction queue as unreliable: draft
  the parameters (symbol/action/qty/type/price/TIF) for manual TWS entry
  and skip the tool, unless the owner reports it working.
- **IB Gateway has NO order-entry UI.** It is an API bridge only — no
  ticket, no watchlist, nothing to click. If the owner says "I don't see
  it", check *which app is open*: on 2026-08-07 they had switched to
  Gateway, which also logged TWS out (one login per username), so the
  ticket they were looking for could not exist. The fix is to close
  Gateway and reopen TWS.
- **A stale limit is the most common self-inflicted delay.** QQQ moved
  $716.96 → $722.74 (+0.8%) during roughly an hour of troubleshooting and
  the order had to be repriced four times. Since a buy limit fills **at
  the ask**, not at the limit, a wide buffer (~1% above the ask) is nearly
  free — it costs only a slightly smaller share count, because sizing is
  computed against the worst-case fill. Prefer the wide buffer.
- **`examples/place_core_holding.py`** (added 2026-08-07) sidesteps the GUI
  entirely: it reads live settled cash, sizes through `size_core_holding`,
  prices off the live ask, refuses to run outside the regular session, and
  submits only on a typed YES. Its offline logic is tested; its final
  `placeOrder` call has NOT been exercised yet.
- **The local TWS socket path WORKS — verified 2026-08-07** by the owner
  running `check_tws.bat` → `examples/tws_status.py` successfully. That
  proves the whole prerequisite chain on their machine: API socket enabled,
  127.0.0.1 trusted, port correct, venv has `ib_async`. So local scripts are
  the reliable execution route, in contrast to `create_order_instruction`,
  which has never filled. `tws_status.py` also uses `reqAllOpenOrders`, so
  it sees hand-typed GUI tickets that `get_account_orders` misses — prefer
  it over the connector when auditing working orders.

### Two things learned while placing the first core order (2026-08-05)

**"Never take a loss to make a trade."** The owner stated this preference
when asked about closing F (then −$2.22 unrealized). It is worth answering
carefully rather than just complying, because the reasoning is a known and
expensive bias (the disposition effect): an unrealized loss is *already*
reflected in net liquidation, so selling converts "unrealized" to
"realized" without changing the dollar value of the account. **This is a
TFSA, so there is additionally zero tax difference between the two.** The
decision-relevant question is only ever "which asset has the better
expected return from here", never "what would this do to my cost basis".
Evidence presented at the time: F returned +8.9% over 5 years (1.7%/yr,
~5.9%/yr including its 4.21% dividend) versus QQQ's +94.5% (~14.7%/yr),
and F alone was 43% of the account — double the single-issuer cap in
rule 1. Say this plainly if it comes up again; don't quietly comply.

**Size the buy AFTER the sells fill, never off projected proceeds.** The
first QQQ draft was sized off expected sale proceeds and would have
breached the 70% cap if F/JPM had filled below quote — caught only by
re-checking against `size_core_holding` across a range of fill prices.
Sale proceeds are unknown until filled, so either wait for the fills and
size off real cash, or size against the *worst* plausible fill. Under-
deploying is free; breaching the cap is not.

**Check the market clock BEFORE drafting, and verify state instead of
trusting a report of it.** Two rounds of DAY-order drafts were priced and
re-priced against live quotes on 2026-08-05 without noticing the session
was minutes from closing; all were deleted unfilled at 16:11 ET. Rules:
DAY drafts are worthless outside 09:30-16:00 ET, and a limit priced off a
16:00 quote is stale against the next open — redraft at the open rather
than leaving one queued overnight. Never draft an entry that spends cash
the account does not yet hold: if a funding sell and a funded buy are both
queued, an overnight gap can fill the buy while the sell misses,
overdrawing the account. Sequence them, don't stack them. And confirm
fills against `get_account_trades` + `get_account_positions` — an
instruction sitting at `is_new: true` was never submitted, and live orders
from previous sessions (e.g. GTC stops) stay working until explicitly
cancelled *in the app*: there is no cancel/modify tool on the MCP
connector, so Claude cannot clear them.

### TWS order-entry gotchas (learned expensively, 2026-08-06)

- **`get_account_orders` UNDER-REPORTS working orders.** It showed 2 rows
  while TWS showed 6, and missed a 100-share order entirely. **The TWS
  Orders tab is authoritative; the connector is not.** On 2026-08-06 Claude
  built a theory that the connector was stale and told the owner their
  stops were cancelled when they were live — the connector had been right.
  When the connector and the owner's screen disagree, ask for a screenshot;
  do not pick whichever supports the current theory.
- **TWS will not change Order Type on a working order.** The Order Type
  dropdown is greyed out on an existing ticket — only price fields are
  editable. Editing a STP's price three times leaves it a STP. Changing
  STP→LMT requires cancel + fresh order. A sell *stop* above the market is
  actively harmful: it converts to a market order the moment price rises
  into it.
- **The Order Entry panel pre-fills QTY 100.** This produced a live
  `SELL 100 JPM LMT 357.00` — ~$35,700 against a $296 account and a 0.09
  share position — which would have been a naked short in a TFSA that
  cannot short. **Always check the value estimate next to SUBMIT** (`≈ 32.13
  USD`, not `≈ 35.7K USD`) before transmitting. That figure catches a wrong
  quantity faster than reading the qty field.
- **Price sell limits BELOW the bid, never at it.** A limit at the bid goes
  un-marketable on a single downtick; four successive F drafts missed this
  way as the bid drifted a cent at a time. A sell limit below the bid still
  fills AT the bid — the limit is a floor, not the sale price — so the
  buffer is free. Same logic inverted for buys.
- **Duplicate orders accumulate silently.** Repeated attempts left three
  identical `F STP 13.70` orders (27 shares against a 9-share position)
  plus a stale `F LMT 14.60` that was holding the shares and blocking every
  new sell. When orders behave inexplicably, list ALL working orders first
  — `examples/cancel_open_orders.py` (dry run) reads TWS directly — and
  prefer cancelling everything and rebuilding over patching a bad list.

## Execution policy

- **Claude never places live orders autonomously.** Orders are drafted
  (IBKR MCP `create_order_instruction`, which requires the owner to review
  and submit in the IBKR app) or proposed as `ibkr/orders.py` calls that
  require an explicit `confirm=True` from the person running them. This
  includes existing positions — analysis and recommendations yes,
  unilateral buying/selling/shorting no.
- Prefer limit orders over market orders on the live account.
- **Unattended/hands-free automation is allowed ONLY against the paper
  account** (`examples/autotrade_paper.py`: TWS paper port 7497, aborts
  unless every managed account id starts with "D"). The live account
  always keeps a human tap between signal and execution.
- The live TWS socket is 127.0.0.1:7496; scripts here only work while the
  owner's TWS is open and logged in.
- **`service/` (added 2026-08-03)** — a Windows background service
  (`supervisor.py`) that runs the scan/score/risk pipeline continuously,
  with reconnect-with-retry, crash recovery, health monitoring, and
  rotating logs. `MODE=PAPER` is fully autonomous, same guard as
  `autotrade_paper.py`. `MODE=LIVE` runs the full pipeline against the
  live account and QUEUES qualifying setups to `journal.pending_orders` —
  it does not call `ib.placeOrder`/modify/cancel under any mode setting;
  there is no code path in this service that submits a live order without
  a human running `examples/review_pending_orders.py` and typing YES per
  order. This was explicitly requested and explicitly declined during
  development — do not add one. See `docs/SETUP_WINDOWS.md`.
- **The scan loop is DISABLED by default (owner decision, 2026-08-07).**
  `SCAN_ENABLED` gates the pipeline independently of `MODE` and is opt-IN:
  unset, empty, or unrecognized all leave it off, so a typo fails safe.
  With it off the supervisor still connects, reconnects and health-checks
  but never calls `run_cycle` in either mode. Rationale: the gate and
  pullback strategies both backtested to negative expectancy, and the
  adopted strategy (QQQ buy-and-hold) is deliberately inert — a running
  scanner would only generate alerts for setups already decided against.
  Locked by `tests/test_service_config.py`. Turn it back on only for a
  strategy re-validated through `net_of_commission.py`.

## Account facts (verified, stable)

- **The owner is in Vancouver, British Columbia — Pacific time**
  (America/Vancouver, PDT/PST). This container runs on **UTC**, so neither
  the owner's clock nor the container's is the market clock. US regular
  session 09:30-16:00 ET = **06:30-13:00 PT** — the close lands early
  in the owner's afternoon. Always convert explicitly (America/New_York
  for the market, America/Vancouver when speaking to the owner) and
  re-read the current date on every wake: sessions here span days, and
  assuming "now" is unchanged since the last message produced a wrong
  "market is closed" call on 2026-08-06.
- **TFSA, base USD, LONG-ONLY** — cannot short. SELL/short signals from the
  gate are journaled but must never be traded or drafted.
- **Fractional shares are enabled** and are essential: the account is ~$300,
  so a 20%-of-funds slice is ~$28 and can't buy one whole share of most
  names. Always pass `fractional=True` to `size_position` for live sizing.
- **One IBKR login per username.** Desktop TWS and the phone app both use
  `alcademe67` and kick each other off. A second username (`alcademe6767`)
  has been pending IBKR identity verification since 2026-07-23 — check
  Client Portal → Message Center for a "Document Required" flag. Until it
  activates, TWS cannot stay connected while the phone app is open.
- Claude's IBKR MCP connector is a **separate session** from the owner's
  logins — querying the account never disconnects their TWS or phone.

## Environment gotchas (learned the hard way, 2026-08-03/05)

- **Cron jobs are session-only and get wiped constantly** — often within
  minutes, and always when the session ends. `CronList` before assuming any
  watch is armed; re-arm on every session start. There is no durable
  scheduler here. The reliable path is the Windows service in `service/`.
- **Egress blocks a lot of the web.** Confirmed 403: Yahoo Finance /
  yfinance, Wikipedia, humbledtrader.com. Market data must come from the
  IBKR connector. WebSearch works and is the tool for news vetoes.
- **Subagents must never call sleep/wait/Monitor** — foreground waiting is
  blocked and hangs the agent until it dies. Tell them explicitly to retry
  failed calls immediately with no pause.
- **The IBKR connector rate-limits under parallel load.** ~5 concurrent
  subagents is workable; 10 causes stalls and truncated/scrambled writes.
  Have agents work one ticker at a time (fetch → write → verify → next) and
  verify array-length consistency per file; batching many fetches before
  writing produces cross-contaminated data.
- **Long fan-outs hit session token limits** (resets are several hours
  apart). Check coverage and gap-fill rather than restarting a whole batch.

## Data caches (`data/`, gitignored — moved out of the scratchpad 2026-08-13)

Caches live in the working tree at `data/<span>/SYMBOL.json`, resolved by
`paths.data_dir()` (override with `$MEDIK_DATA_DIR`). They used to live in a
session scratchpad, and the three backtest runners hardcoded an absolute path
to one; scratchpads are deleted with the container, so **the backtests behind
the adopted strategy were unrunnable** until this moved. Don't reintroduce a
scratchpad path in committed code.

- `data/data2y/` — ~2-year daily bars, ~200 names, for the gate/pullback
  backtests. Known bad in the original fetch: LUNR, REGN, TMO (ragged arrays
  — refetch); CFLT, BITF (no resolvable US listing); ALB, AAL, DAL, UAL, CCL,
  NCLH (not fetched). SNDK and WOLF have <2y of genuine history
  (spinoff/reorg) — not corrupt.
- `data/data5y/` — ~5-year ETF bars for `backtest/lowfreq.py`.
- Fill either with `python examples/fetch_bar_cache.py <cache> [--universe|--etfs]`
  (needs TWS open; resumable, so rerun to fill gaps).
- Read them with `ibkr.cache.load_cache` — **not** a hand-rolled loader. It
  centralizes the validation below, and returns a `skipped` list so a run
  can't quietly cover a smaller universe than it claims.
- `<scratchpad>/eval_both.py` — evaluates one symbol against BOTH strategies
  and enforces all sizing/R:R rules; used by the morning-scan cron. Still
  scratchpad-only, so it dies with the session.
- **Always validate a cache file before scoring**: all six OHLCV+time arrays
  must be equal length. A mid-session partial bar cached and reused after
  close silently produced a false gate failure on 2026-08-03 — that's why
  `strategy/data_quality.py` exists. `ibkr.cache` enforces the length check
  on load and writes via temp-file-and-rename so an interrupted write can't
  produce a ragged file; completeness of the trailing bar is still the
  caller's job, passed in as `transform=`.

## Alerts

When a scan produces a qualifying signal during a Claude session, push it to
the owner's phone with the PushNotification tool: symbol, side, entry, stop,
target, quantity. One line, no fluff. Note the background Windows service
**cannot** reach the phone this way (no Claude session) — it falls back to a
Windows toast + `logs/alerts.log`, or SMTP if configured.

## Layout

- `ibkr/` — TWS connection (`client.py`), order helpers (`orders.py`),
  historical data (`data.py`), market scanner (`scanner.py`)
- `strategy/` — indicator math (`indicators.py`), signal gate
  (`signals.py`), trend-pullback strategy (`pullback.py`), candidate
  scoring (`scoring.py`), position sizing/risk (`risk.py`), portfolio
  circuit breakers (`risk_limits.py`), in-trade management:
  breakeven/trailing/partial-profit (`trade_management.py`),
  bar-completeness validation (`data_quality.py`), universe list
  (`universe.py`), config, and the SQLite decision journal (`journal.py`,
  → `journal.sqlite` at repo root, gitignored)
- `backtest/` — no-lookahead multi-symbol backtester (signal on close,
  fill at next open, stop-first when stop and target share a bar), plus the
  commission gate (`net_of_commission.py`) and the low-frequency comparison
  (`lowfreq.py`, `run_lowfreq_comparison.py`)
- `examples/` — runnable entry points (`connect_test.py`, `run_scan.py`,
  `run_backtest.py`, `manage_open_positions.py`, `check_risk_limits.py`,
  `show_journal.py`, `review_pending_orders.py`, `fetch_bar_cache.py`)
- `service/` — Windows background service: `supervisor.py` (main loop),
  `pipeline.py` (shared scan-score-risk-act cycle, mode-aware),
  `config.py`, `health.py`, `alerts.py`, `market_hours.py`,
  `logging_setup.py`, `run_supervisor.bat`
- `paths.py` — repo-root module resolving `data/`, `reports/`, `logs/`,
  `notebooks/` (env overrides `MEDIK_DATA_DIR` / `MEDIK_REPORTS_DIR` /
  `SERVICE_LOG_DIR` — the last is the service's existing variable, reused
  rather than duplicated, and `service/config.py` now delegates to it)
- `docs/` — operating docs: `RESTART_PROMPT.md` (session handoff),
  `SETUP_WINDOWS.md` (service install), `core-holding-runbook.md`,
  `backtest-verdict.md` (what was tested + how to reproduce it).
  MCP-server setup lives in `README.md`, not here
- `data/` — cached daily bars, **gitignored** (README tracked)
- `reports/` — generated report output, **gitignored** (README tracked);
  contains live balances, which is why it stays out of git
- `logs/` — service logs (`service.log`, rotating 5MB×5) and `alerts.log`
  (append-only, never rotated), **gitignored** (README tracked). A quiet
  log is expected while `SCAN_ENABLED` is off — that is the configuration,
  not a fault
- `notebooks/` — research notebooks, tracked; commit with output cleared
- `.github/workflows/tests.yml` — CI on every PR: pytest on Linux **and
  Windows** (py3.11/3.12) plus a byte-compile pass over `examples/` and the
  backtest runners, which no test imports. A second job installs pandas but
  **not** `ib_async`, locking in the property that reading a bar cache never
  drags in the broker library — don't "fix" that job by adding `ib_async`
  to it; if it fails, an eager import crept back in
