# Core holding runbook — QQQ

The account's core strategy. **`CLAUDE.md` is the authority**; this is the
operational how-to.

## Current position

Opened 2026-08-07, 08:27 PT / 11:27 ET:

```
BUY 0.2836 QQQ @ $722.74    commission $1.00    net $204.97
```

Cost basis **$726.27/share** including commission. At entry: 69.9% of
available funds (ETF cap 70%), 70.2% of equity deployed (portfolio cap 80%),
$87.21 cash, $292.17 net liquidation.

**No stop. No target. No sell trigger.** That is the strategy, not an
oversight.

## Checking it

```bash
python examples/check_core_holdings.py             # print
python examples/check_core_holdings.py --save      # also write to reports/
python examples/check_core_holdings.py QQQ 722.74  # headroom in shares
```

Read-only — it never drafts an order. It reports position value, both caps,
remaining headroom, and a drawdown figure.

**The drawdown line is not a sell signal.** QQQ fell 22.9% inside the
five-year window that returned +121.8%, and 35.6% in 2022. Selling into
either is how a holder converts a winning strategy into a losing one.

## Adding to it

Sizing goes through `strategy.risk.size_core_holding` — a **separate path**
from `size_position`, not a flag on it. Buy-and-hold has no stop, and
`size_position` derives both the target and the size *from the stop
distance*, so it structurally cannot express this position.

Rule 5 (max 1% of net liquidation at risk) is **inapplicable here, not
waived**: "capital at risk" means "dollars lost if the stop fills", and with
no stop that quantity is undefined. `CoreHoldingPlan` has no such field —
don't invent one. Risk is controlled by diversification (100 companies), the
70% cap, and the 20% cash floor.

The drawdown circuit breakers are also **deliberately not applied**. They
exist to stop a swing trader revenge-trading a losing streak; applied to a
decades-horizon index entry they would forbid buying *during a dip*.

```bash
python examples/place_core_holding.py
```

Reads live settled cash, sizes through `size_core_holding`, prices off the
live ask, refuses to run outside the regular session, and submits only on a
typed YES. Its offline logic is tested; **its final `placeOrder` call has not
been exercised yet** — watch it the first time.

### Size after the sells fill, never off projected proceeds

The first QQQ draft was sized off *expected* sale proceeds and would have
breached the 70% cap had F/JPM filled below quote. Sale proceeds are unknown
until filled: either wait for the fills and size off real cash, or size
against the worst plausible fill. Under-deploying is free; breaching the cap
is not. Never queue a funding sell and a funded buy together — an overnight
gap can fill the buy while the sell misses.

## When to sell

Buy-and-hold has **no sell rule derived from price**. It has sell reasons
derived from the owner's life. There are exactly three:

1. **The money is needed.** Sell at whatever price exists that day. Ordinary,
   requires no analysis.
2. **The holding stops being what was bought** — the fund is liquidated or
   restructured, or the index methodology changes such that it is no longer a
   broad, unleveraged, diversified fund. *A 30% decline is not a thesis
   break; it is what the asset does.*
3. **A structurally better fund is chosen** — e.g. VOO/VTI for broader
   coverage than the tech-heavy Nasdaq-100. A calm portfolio-construction
   decision made once, not a timing call. Both are already whitelisted in
   `strategy/core_holdings.py`.

**Not reasons to sell:** it fell; it rose a lot; it "looks toppy"; a scary
headline; locking in gains; hitting a round percentage; a drawdown line in
the report; anyone forecasting a crash.

If the money is needed within **~3 years it should not be in QQQ at all** —
cash or a GIC.

## Refuse to attach sell orders to this position

No take-profit limit, no protective stop.

- A +10% take-profit inside the tested window would have exited within months
  and forgone the remaining ~112 percentage points.
- Any meaningful stop would have sold at the bottom of the 22.9% drawdown the
  position fully recovered from.

Both convert buy-and-hold into timing, which lost in every configuration
tested. GTC orders also linger and **cannot be cancelled from Claude's side**
— there is no cancel/modify tool on the MCP connector — so a forgotten sell
order is a durable hazard.

## Order-entry gotchas (learned expensively)

- **`create_order_instruction` has never produced a fill.** Across two
  sessions every instruction sat at `is_new: true`. Draft the parameters for
  manual TWS entry instead.
- **The local TWS socket path works** — verified 2026-08-07 via
  `check_tws.bat` → `examples/tws_status.py`. Local scripts are the reliable
  route. `tws_status.py` uses `reqAllOpenOrders`, so it sees hand-typed GUI
  tickets that `get_account_orders` misses — prefer it when auditing.
- **`get_account_orders` under-reports.** It showed 2 rows while TWS showed
  6. **The TWS Orders tab is authoritative.** When the connector and the
  screen disagree, ask for a screenshot; do not pick whichever supports the
  current theory.
- **IB Gateway has no order-entry UI** — it is an API bridge only, and
  opening it logs TWS out (one login per username). If the owner says "I
  don't see the ticket", check which app is open.
- **The Order Entry panel pre-fills QTY 100.** This produced a live
  `SELL 100 JPM LMT 357.00` against a 0.09-share position. **Always check the
  value estimate next to SUBMIT** (`≈ 32.13 USD`, not `≈ 35.7K USD`) — that
  catches a wrong quantity faster than reading the qty field.
- **TWS will not change Order Type on a working order.** STP→LMT requires
  cancel + fresh order.
- **Price buy limits above the ask, sell limits below the bid.** A buy limit
  fills *at the ask*, so a ~1% buffer is nearly free — it costs only a
  slightly smaller share count. A limit *at* the quote goes un-marketable on
  a single tick; QQQ moved +0.8% during an hour of repricing on 2026-08-07.
- **Duplicate orders accumulate silently.** When orders behave inexplicably,
  list them all first (`examples/cancel_open_orders.py` dry-runs against TWS)
  and prefer cancelling everything and rebuilding.

## "I bought high"

Said minutes after the fill (QQQ +1.14% on the day, filled 6c off the day
high). The arithmetic: the entry was $5.78 above the session low, which on
0.2836 shares is **$1.64**. Selling to re-buy costs **$2.00 in commissions
alone** — fixing it costs more than the error, before considering that the
re-entry might be higher still.

Also: the 52-week range was $555.60–$748.65, so the fill was 3.5% *below* the
52-week high. "Day high" is not "high". And entry timing is specifically what
the research found doesn't pay — the 200-day timing overlay, a disciplined
attempt to buy lower, cost 3.6%/yr versus buy-and-hold.

Answer it the same way if it recurs: quantify the gap, quantify the cost of
correcting it, and say plainly that intervening is the failure mode of this
strategy.
