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
   (adopted 2026-08-04, see rule 8).
   **2a. EXCEPTION (owner decision, 2026-08-27) — manual swing drafts only:**
   for 1-5 day ETF swing tickets that Claude DRAFTS and the owner personally
   clicks in TWS, the owner accepts **R:R ≥ 1:1 measured against a fixed
   −2.5% stop**, and single-position sizing up to ~90% of cash (superseding
   the 20% cap for these hand-clicked trades). Scope is exactly that: drafts
   with a human tap. The automated bot's gates and every other use of rules
   1/1b/2 are unchanged. Provenance: the owner's stated style — hold days,
   take $10-20, rotate — after hearing the cost arithmetic and the negative
   swing backtests; recorded, not endorsed by any new test. Either way the target must clear its
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
active (`<scratchpad>/run_pullback_bt.py`, `net_of_commission.py`).

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
  always keeps a human tap between signal and execution — with ONE
  explicit, owner-authorised exception, below.
- **EXCEPTION — `examples/medik_etf_live.py` submits live orders
  automatically** (owner decision, 2026-08-21). Once `MEDIK_ETF_LIVE=true`
  it runs a continuous market-hours loop and sends qualifying ETF orders
  with no per-order confirmation. The owner asked for this specifically and
  in detail, overriding their own earlier rule for this strategy alone.
  **The scope is exactly one file.** Every other order path in this repo
  still requires a human: `ibkr/orders.py` keeps `confirm=True`,
  `place_core_holding.py` and `medik_mtf_live.py` keep their typed prompts,
  and `service/` still only QUEUES. Do not generalise this exception, and
  do not add an equivalent to any other strategy without the owner asking
  for it as explicitly as they did here.
  **What replaced the human tap** — not nothing, but
  `strategy.medik_etf.authorize_order()`, a pure function running 18
  deterministic checks (live flag, connection, account data, buying power,
  market data, setup validity, whole-share size, stop, target, reward/risk,
  risk ceiling, capital utilisation, conflicting position, conflicting
  order, duplicate suppression, session gates, entries-enabled). Identical
  inputs always produce an identical decision, and **no model output ever
  authorises a trade** — that was a stated requirement and it must stay
  true. Risk controls are unchanged: 0.5% risk per trade with a 1.0%
  ceiling, ATR stop, 1.5R target, whole shares only, one position, three
  trades per session, 2% daily loss limit, and mandatory bracket
  verification that flattens the position if a protective leg fails.
  `MAX_CAPITAL_UTILIZATION = 0.90` is an ALLOCATION ceiling, not a loss
  limit — the risk ceiling binds independently and whichever is tighter
  wins, so raising it cannot increase dollars at risk.
  **The expectancy caveat still stands.** This strategy has never been
  backtested; the related MTF strategy tested to −41%/yr at ~$290 of
  equity, and nothing about automating execution improves expectancy. Say
  so if asked whether to run it.
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
  development — do not add one. See `service/SETUP_WINDOWS.md`.
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

## MARKET DATA — the licence boundary (settled 2026-08-26, read before debugging quotes)

**The account HAS real-time US data. The TWS socket API cannot use it.** This
took most of a day to establish; do not re-derive it.

`US Real-Time Non Consolidated Streaming Quotes` is active on U26953060, fee
waived (Client Portal shows six subscriptions, **Total USD 0**). Verified
REALTIME through the IBKR **Client Portal Web API** for SPY (ARCA), TQQQ
(NASDAQ) and SNXX (BATS) — all three networks the ETF universe spans.

Through the **TWS socket API** the same symbols return **error 10089:
"Requested market data requires additional subscription FOR API"**. That
wording is literal: IBKR licenses this free feed for its own platforms (TWS,
mobile, Client Portal) and treats the socket API as third-party
redistribution. Live prices can therefore be on screen in TWS while the API
is refused. **No setting grants API rights to a platform-only feed** — an
earlier version of `mktdata_probe.py` claimed one existed, which was wrong
and sent the owner hunting for a toggle that does not exist.

Tested and ruled out, so nobody repeats it:

| hypothesis | result |
|---|---|
| snapshot vs streaming | both fail at type 1 (`reqTickers` is snapshot, and is *also* separately entitled) |
| direct venue routing (IEX/BATS/ARCA/NYSE/ISLAND) | **all fail identically**, each naming the PRIMARY exchange — IBKR resolves past the requested venue |
| subscription missing | no: active, fee waived, Total USD 0 |
| delayed (type 3) | works — and `read_quote()` REFUSES it on purpose |
| historical (`reqHistoricalData`) | works, entitled separately — which is why bars load while quotes fail |

**The fix, already built:** `ibkr/cpapi.py` + `MEDIK_ETF_QUOTE_SOURCE=cpapi`.
Quotes from the Client Portal Web API; orders and historical bars still via
TWS. Needs IBKR's **Client Portal Gateway** running locally (separate
download; `bin\run.bat root\conf.yaml`; log in once at
https://localhost:5000). That session is browser-authenticated and expires
more often than TWS's weekly re-login, so `auth_status()` is checked at
startup and a dead session exits at preflight rather than trading on nothing.

`CpQuote` is duck-typed to ib_async's `Ticker` so `read_quote()` applies the
SAME gates to both providers. Field 6509 maps onto the TWS `marketDataType`
numbering; an unknown or absent availability code maps to **delayed, not
real-time**, because a quote whose origin cannot be established must never
reach a live order. IBKR also returns `"C123.45"` in the same field as a live
price to mean "derived from the previous close" (and `"H"` for halted) —
both are rejected rather than stripped and parsed.

**2026-08-26 (laptop session) — the cpapi fix CONFLICTS with TWS on one
username.** Logging into the local Client Portal Gateway (Firefox,
12:30:11 PT) bumped TWS's session — TWS's own dialog said "EXISTING SESSION
DETECTED", naming the gateway login's IP and timestamp. From that moment
TWS's API wedged: `reqCurrentTime` answered while order/account requests
hung forever, which froze the ETF bot at startup reconciliation. Clicking
"Reconnect This Session" in TWS revived it and killed the gateway session
in return. While both were briefly up, the gateway served quotes flagged
`DB`/delayed (`marketDataType 3`) even as the MCP connector showed
`REALTIME` for the same symbol in the same minute — the free feed's
real-time seat stays with the other sessions. **Net: with one username
there is NO configuration giving the bot both an order path (TWS) and a
real-time quote path (gateway)** — socket real-time is 10089-blocked, and
a gateway login takes TWS's session. The unattended run therefore exits at
the quote-session preflight: safe, idle, correct. Unblocks, in order:
(1) activate the second username `alcademe6767` (identity verification
pending since 2026-07-23 — Client Portal → Message Center) and run the
gateway on it while TWS keeps `alcademe67`; (2) pay for an API-entitled
market-data subscription so the TWS socket itself serves real-time —
verify with IBKR that the specific subscription extends API rights BEFORE
paying, given the 10089 history; (3) accept the idle bot.

**Message Center checked 2026-08-26 (owner logged into Client Portal, Claude
navigated):** there is **NO pending verification** for `alcademe6767` — Pending
Tasks empty, no "Document Required", no Users & Access Rights section, and the
Market Data Subscriptions username selector shows exactly ONE username
(`alcademe67`). The stale "second username pending since 07-23" note is
resolved history: the July application most plausibly became the second
ACCOUNT U26920266 (Individual, appeared 2026-08-24) under the same single
username. A second username would need a FRESH request.
**Requested 2026-08-26: IBKR web ticket #243901** (Account Services →
Account Configuration/Permissions) asking IBKR to create the second
username or enable the missing Users & Access Rights section — the
self-service path IBot describes does not exist on either account.

## ACTIVATION PROTOCOL (owner order, 2026-08-31 — SUPERSEDES the runbook below)

The owner has authorized REAL-MONEY automated ETF trading, gated on the
secondary username and an EXPLICIT activation phrase. Until IBKR approves
`alcademe0209`: do NOT submit ETF orders, do NOT enable the `MEDIK ETF AUTO
TRADER` scheduled task (it is deliberately DISABLED — leave it so), do NOT
start the Client Portal Gateway, no test orders, no account changes, no
strategy/risk parameter changes. Allowed meanwhile: read-only verification
(TWS config, code, live config, API config, market-data requirements, risk
controls), technical bug fixes, and keeping everything ready.

When the username flips to Active: do NOT activate anything. Report
"SECONDARY USERNAME APPROVED", then verify WITHOUT placing orders:
(1) alcademe67 → TWS, (2) alcademe0209 → gateway, (3) both sessions
coexist, (4) correct account U26953060 visible, (5) ETF market data
available — note the account-level snapshot/equity-minimum issue may still
force delayed data, (6) live API connection works, (7) risk controls pass,
(8) strategy preflight passes, (9) scheduled task ready (still disabled).
Then report "MEDIK ETF AUTO TRADER READY FOR LIVE ACTIVATION" and WAIT.
Only the owner's exact phrase "ACTIVATE MEDIK ETF LIVE" authorizes
enabling the task / arming the engine, and the full live preflight runs
before any first order. The order engine stays OFF until that phrase.

**SECOND USERNAME EXISTS: `alcademe0209` — status PENDING as of
2026-08-26** (owner's report). Historical runbook below for reference —
the PROTOCOL ABOVE governs where they differ (notably: nothing activates
automatically anymore):

1. **Log the Client Portal Gateway in as `alcademe0209`** — never
   `alcademe67` — at https://localhost:5000 (start it with
   `clientportal.gw\start_cpgw.bat` if it isn't running). This is the
   entire point of the second username: the gateway holds ITS session
   while TWS keeps `alcademe67`, so neither bumps the other.
2. **Enable market data for `alcademe0209`**: subscriptions are
   per-USERNAME. In Client Portal → Settings → Market Data Subscriptions
   (scoped to the new username), add `US Real-Time Non Consolidated
   Streaming Quotes` (fee waived). Without this the new session serves
   delayed and `read_quote()` refuses it.
3. **The equity minimum still binds** (Message #M82996142, 2026-08-14:
   snapshot capability disabled below the market-data minimum equity,
   commonly USD 500; account is ~$286). If quotes still come back
   `DB`/delayed after steps 1–2, this is the remaining lever — the
   account needs topping up. Account-level, so the new username does not
   bypass it.
4. **Verify with the bot itself**: `MEDIK_ETF_DRY_RUN=true` +
   `MEDIK_ETF_QUOTE_SOURCE=cpapi` — startup must log `QUOTE SESSION: OK`
   and per-symbol quotes without delayed-data rejections. No code or
   config changes are needed: `run_medik_etf.bat` already points at the
   gateway, and nothing in the repo names the username.
5. TWS stays as it is (auto-restart 06:00, `alcademe67`); the scheduled
   task picks everything up automatically — the self-healing wrapper
   retries every 5 minutes, so a gateway logged in mid-session is
   adopted without touching anything.

**And a second, independent quote blocker found (Message #M82996142,
2026-08-14):** "your eligibility for requesting snapshots was disabled"
because U26953060's equity fell below IBKR's market-data minimum equity
requirement. The cpapi path uses `/iserver/marketdata/snapshot`, so this
plausibly explains why the gateway served `DB`/delayed on 2026-08-26 even
while authenticated. Restoring it means bringing equity back above IBKR's
minimum (commonly USD 500; verify the current figure with IBKR). So the
full unblock for automated trading is BOTH: (a) equity above the market-data
minimum, and (b) a second username (or API-entitled paid data) so quotes and
TWS can coexist. Neither exists today; the bot idles safely at preflight.

## COST ARITHMETIC — why activity is the enemy here (2026-08-26)

Commission is `clamp($0.005/share, min $1.00, max 1% of value)`. At this
account's position sizes the **$1.00 minimum binds**, so every round trip
costs **$2.00** regardless of symbol. Risk budget is 0.5% of $286 = **$1.43**.

**Cost is 1.40× the entire risk budget per round trip.** At a 1.5R target a
winner nets $0.15 and a loser costs $3.43 — **break-even needs a 96% win
rate**. This is arithmetic, not strategy, and it is the same force that made
the gate (−8.3%) and pullback (−35.9%) strategies lose.

| equity | risk/trade | rt cost | cost/R | breakeven win% |
|---|---|---|---|---|
| $286 | $1.43 | $2.00 | 1.40 | **96% — impossible** |
| $1,000 | $5.00 | $2.00 | 0.40 | 56% |
| $2,000 | $10.00 | $2.00 | 0.20 | 48% — viable |
| $3,000 | $15.00 | $2.00 | 0.13 | 45% — viable |

Intraday momentum becomes mathematically possible around **$2,000**. Below
that, no entry rule, score or exit overcomes the cost floor. Three trades a
day at $286 is $126/month in commissions — 44% of the account per month.
State this whenever "make it more active" comes up; the owner has heard it
and may still choose to proceed, which is their call.

## ETF INTRADAY BACKTEST VERDICT (2026-08-26) — RED at every size, both versions

First-ever run of `backtest/medik_etf_bt.py` (item 4 of the 08-26 handoff):
6 months of real 5-minute IBKR bars, 2026-02-23 → 2026-08-26, 8 symbols,
~10,040 bars each, real commission schedule, stop-first fills, spread and
slippage modelled. Promotion gates evaluated on the out-of-sample window
(final 40%, parameters untouched). Every configuration failed:

| run | net P&L (full period) | OOS net PF | OOS expectancy | verdict |
|---|---|---|---|---|
| v2 @ $290 | −$49.99 (−17.2%) | 0.25 | −$0.82/trade | RED |
| v2 @ $500 | −$129.56 (−25.9%) | 0.26 | −$1.95/trade | RED |
| v2 @ $1,000 | −$492.22 (−49.2%) | 0.38 | −$2.45/trade | RED |
| v2 @ $2,500 | −$1,396.36 (−55.9%) | 0.53 | −$3.49/trade | RED |
| v2 @ $5,000 | −$2,376.61 (−47.5%) | 0.63 | −$4.97/trade | RED |
| v1 @ $500 | −$636.04 (−127.2%) | 0.11 | −$1.94/trade | RED |

Two findings, and the second kills the it-works-at-scale hypothesis:

1. **v1 is catastrophic.** 19.1% win rate over 320 trades; $564.64 of
   commission against $500 of starting equity; the simulated account went
   below zero. v1 is what `examples/medik_etf_live.py` trades when armed.
2. **There is no gross edge at any size.** The COST ARITHMETIC table above
   assumed the strategy wins before costs and commission is the only
   obstacle. Measured, gross profit factor is 0.69–1.01 across every run —
   the strategy loses BEFORE commission once enough signals clear the
   whole-share and net-edge gates. More equity admits more qualifying
   trades and loses more dollars. Scaling the account does not rescue it;
   the "viable around $2,000" line in the cost table is about cost drag
   only and must not be quoted as a promise that the strategy works there.

Consequences: do not arm `medik_etf_live.py` (either version) as
configured. Do not retune parameters against this out-of-sample window —
the backtester's own warning stands: that converts OOS into in-sample and
no clean test remains. A new idea needs a fresh spec and a fresh test
through this same harness before anyone calls it working.

**Live bot switched to the v2 configuration (owner instruction,
2026-08-26: "switch bot to v2 universe but keep commissions
cost-efficient").** `examples/medik_etf_live.py` now scans `V2_UNIVERSE`
(12 liquid non-inverse ETFs), applies BOTH v2 gates before
`authorize_order` — `qualifies_v2` (score ≥ 85 + pullback/reclaim) and
`net_edge_check` (target must clear the full round-trip cost by 1.5×,
which is the commission-efficiency rule) — and uses the 30-minute v2
re-entry cooldown. Startup reconciliation still recognises v1-only
symbols so a legacy inverse-fund position is adopted, not orphaned.
Pinned by `tests/test_medik_etf_v2_wiring.py`. Said once and honestly:
this is the SAME v2 configuration the table above tested — the switch
reduces trade count and refuses cost-losing entries, but the RED verdict
is unchanged; nothing here creates an edge.

Footnote: several runs printed "N signals but N−1 accounted for" — a
signal whose position was still open when the data ran out landed in no
bucket. Fixed 2026-08-26: counted as "still open at end of data" in the
report. Display-only; the trade simulation was never affected (re-run
output identical line-for-line apart from the accounting block) and no
verdict moves.

## MEDIK SWING VERDICT (2026-08-27) — owner's 1-5 day rotation style: RED

The owner asked for their stated style — hold an ETF 1-5 days, take
$10-20, rotate, fixed −2.5% stop, accept 1:1 to the prior swing high —
to be built and tested. Built as `strategy/medik_swing.py` (imports the
pullback structure checks) and `backtest/medik_swing_bt.py` (single
rotating position, next-open fills, stop-first, real commissions,
spread+slippage). 5 years of real IBKR daily bars, 12 ETFs, includes the
2022 bear.

| window | trades | win% | PF | net @ $286 | maxDD |
|---|---|---|---|---|---|
| full 5y | 177 | 32.8% | 0.44 | **−$258.54 (−90.3%)** | 90.7% |
| OOS (final 40%) | 88 | 40.9% | 0.78 | **−$94.22 (−32.9%)** | 40.3% |
| QQQ buy&hold same window | 1 | — | — | **+$258.50 (+90.3%)** | — |

Cause, visible in the exit ledger (93 stops / 41 targets / 43 time-outs):
the −2.5% stop sits INSIDE the daily noise of the leveraged funds the
R:R ranking preferentially selects, so it fires on nothing 53% of the
time; 1:1 geometry with a ~33% win rate is negative before the $275.88
of commissions. **Do not arm; do not retune the stop/target against this
window** — the OOS is burned the moment it tunes parameters. Any new
variant needs a fresh spec and, ideally, data this window hasn't judged.

## Account facts (verified, stable)

- **The login manages TWO accounts (since 2026-08-24): U26953060 (funded,
  the one that is traded) and U26920266 (empty).** Every ib_async read —
  `accountValues()`, `accountSummary()`, `portfolio()`, `positions()`,
  `openTrades()` — spans the WHOLE login and tags each row with its account.
  Code written when there was one account collapsed that: `{r.tag: r.value}`
  keeps whichever row arrived last, so the funded account read **$0.00 net
  liquidation and $0.00 buying power** and the ETF bot sized against nothing.
  **Set `IBKR_ACCOUNT=U26953060`** (or `MEDIK_ETF_ACCOUNT` for the ETF bot,
  which takes precedence). The rule lives in `ibkr/accounts.py`: one account
  → use it; several → it must be NAMED, and scripts refuse to run rather than
  pick the first. `tests/test_account_scope_repo.py` fails any new script that
  reads across the login or places an order without `order.account`.
  Note `ib_async` only auto-subscribes account updates when the login has
  exactly one account (`connectAsync`: `if not account and len(accounts) == 1`),
  so multi-account scripts must call `reqAccountUpdates(account)` themselves
  or `portfolio()` stays empty and positions fall back to cost basis.

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

## Data caches (scratchpad, not in git)

- `<scratchpad>/data/*.json` — ~1-year daily bars, ~88 names.
- `<scratchpad>/data2y/*.json` — ~2-year daily bars, **~193 of 204** names,
  fetched 2026-08-05 for the pending pullback backtest. Known bad/missing:
  LUNR, REGN, TMO (ragged arrays — refetch before use); CFLT, BITF (no
  resolvable US listing); ALB, AAL, DAL, UAL, CCL, NCLH (not fetched).
  SNDK and WOLF have <2y of genuine history (spinoff/reorg) — not corrupt.
- `<scratchpad>/eval_both.py` — evaluates one symbol against BOTH strategies
  and enforces all sizing/R:R rules; used by the morning-scan cron.
- **Always validate a cache file before scoring**: all six OHLCV+time arrays
  must be equal length. A mid-session partial bar cached and reused after
  close silently produced a false gate failure on 2026-08-03 — that's why
  `strategy/data_quality.py` exists.

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
  fill at next open, stop-first when stop and target share a bar)
- `examples/` — runnable entry points (`connect_test.py`, `run_scan.py`,
  `run_backtest.py`, `manage_open_positions.py`, `check_risk_limits.py`,
  `show_journal.py`, `review_pending_orders.py`)
- `service/` — Windows background service: `supervisor.py` (main loop),
  `pipeline.py` (shared scan-score-risk-act cycle, mode-aware),
  `config.py`, `health.py`, `alerts.py`, `market_hours.py`,
  `logging_setup.py`, `run_supervisor.bat`, `SETUP_WINDOWS.md`
