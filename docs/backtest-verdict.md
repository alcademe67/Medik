# Backtest verdict, and how to reproduce it

`CLAUDE.md` states the conclusion and the rules that follow from it. This
file is the reproduction procedure — what to run, against what data, and how
to read the result honestly.

**Nothing here supersedes `CLAUDE.md`.** If the two disagree, `CLAUDE.md`
wins and this file needs fixing.

## The verdict in one line

Both of this repo's signal strategies are **net-negative** at this account's
size, and every active overlay tested lost to simply holding QQQ.

## What was tested (2026-08-05)

Long-only, full risk engine, fractional sizing, 2 years of real IBKR daily
bars across 201 symbols:

| | gross P&L | commissions | net |
|---|---|---|---|
| pullback (149 trades) | +$4.72 | −$112.47 | **−$107.75 (−35.9%)** |
| gate (45 trades) | +$6.73 | −$31.65 | **−$24.92 (−8.3%)** |

Then five years of ETF bars, same commission model:

| strategy | total | CAGR | maxDD | fills | commission |
|---|---|---|---|---|---|
| **QQQ buy & hold** | **+121.8%** | **17.3%** | 22.9% | **1** | **$1.00** |
| QQQ 200-day timing (monthly) | +90.1% | 13.7% | **13.6%** | 33 | $6.35 |
| dual momentum (quarterly) | +84.7% | 13.1% | 23.3% | 19 | $11.36 |
| SPY buy & hold | +84.0% | 13.0% | 19.0% | 1 | $1.00 |
| SPY 200-day timing | +37.6% | 6.6% | 11.5% | 32 | $9.67 |
| x-sectional stock momentum (best of 4) | **−15.9%** | −3.4% | 43.5% | 59 | $23.24 |

## The two findings, in order of importance

**1. There is essentially no edge, before costs.** Gross profit factor was
1.03 on the pullback strategy: +1.57% over *two years*. Commissions are what
made it catastrophic, but a 1.03 profit factor was never going to survive
anything.

**2. Costs are brutal at this size.** The measured schedule is
`clamp($0.005/share, min $1.00, max 1% of trade value)`. At ~$35 positions
the 1% cap binds on both legs, so **every round trip costs ~2% of position
value**. Scaling doesn't rescue it: gross scales linearly but commissions
floor at $1.00/fill, so break-even lands near a **$19,000 account** — at
which point the strategy earns roughly zero and an index fund earned far
more.

## Caveats — state these whenever the +121.8% is quoted

- The five-year window starts **August 2022, after the 2022 bear** (QQQ
  −33.7%, 35.6% drawdown). That flatters buy-and-hold and under-tests the
  entire purpose of a timing overlay. Re-running from May 2022 did not change
  the ordering, but it is one regime, mostly a tech bull market.
- These are **unadjusted price bars — no dividends**. That *understates*
  buy-and-hold, which holds continuously, versus timing, which sits in cash.
- The Nasdaq-100 fell **83% from 2000–2002** and took roughly fifteen years
  to regain its nominal peak. Nothing here rules out a repeat. The conclusion
  is *not* to add a stop (see `core-holding-runbook.md`) — it is that this
  position should only hold money with a genuinely long horizon.
- Cross-sectional stock momentum is on a ~2-year sample. Indicative only.

This is evidence, not proof.

## Reproducing it

The runners used to read from a session scratchpad that no longer exists, so
these were unrunnable for a while. They now read `data/`.

### 1. Fill the caches (needs TWS open and logged in)

```bash
python examples/fetch_bar_cache.py data2y --universe
python examples/fetch_bar_cache.py data5y --etfs --duration "5 Y"
```

Resumable — rerun to fill gaps. IBKR throttles at roughly 60 historical
requests per 10 minutes; raise `--pace` if you hit pacing violations.

### 2. Run

```bash
# The gate every strategy change must pass:
python backtest/net_of_commission.py 300.43 pullback
python backtest/net_of_commission.py 300.43 gate

# Low-frequency comparison (this is the table above):
python backtest/run_lowfreq_comparison.py 300.43 --save

# Gross-only, for diagnosis; NOT a basis for any conclusion:
python backtest/run_2y_backtest.py 300.43 both
```

Exact numbers will drift from the tables above — the caches will hold more
recent bars than the originals did, so the windows differ.

## The rule this exists to enforce

> Any future strategy change MUST be re-run through `net_of_commission.py`
> before it is described as working. — `CLAUDE.md`

A gross equity curve is not evidence. A backtest that skips symbols without
reporting it is not evidence either — `load_cache` returns a `skipped` list
precisely so a run can't quietly cover a smaller universe than it claims.
