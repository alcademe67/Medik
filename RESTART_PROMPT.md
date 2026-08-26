# Restart prompt — paste into a new Claude session to resume

I'm Ali, in Vancouver (Pacific time). Interactive Brokers TFSA, base USD,
long-only, ~$286. Repo `Medik`, branch
`claude/interactive-broker-python-connect-knsid8`.

**Read `CLAUDE.md` first.** Two sections there will save you a day:
*MARKET DATA — the licence boundary* and *COST ARITHMETIC*. Everything below
assumes you have read them.

## The one operational fact that keeps biting

**The laptop runs stale code.** Repeatedly through 2026-08-26, a fix was
pushed, reported as pulled, and the running file was still hundreds of lines
behind — producing identical symptoms and sending us down dead ends. Before
diagnosing ANY behaviour, confirm what is actually on disk there:

```powershell
cd C:\Users\Administrator\Medik; git fetch origin; git reset --hard origin/claude/interactive-broker-python-connect-knsid8; (Get-Content examples\medik_etf_live.py).Count
```

Or double-click `update_medik.bat`, which does the same and prints PASS/FAIL.
**Do not trust "I pulled it" — trust the line count.** Also note I sometimes
paste your example text back to you rather than real output; ask for a number
or a single word rather than a fill-in-the-blank.

You have no access to my machine. Everything on the laptop needs me at the
keyboard. You can read the account through the IBKR MCP connector, which is a
separate session and, notably, **does return real-time quotes** (see below).

## Where things stand (2026-08-26)

- **Account is FLAT.** $286.15 settled, no positions, no working orders.
- **QQQ core holding was closed 2026-08-24**, realized −$7.03 over 17 days,
  of which $2.71 was commission. The buy-and-hold policy in `CLAUDE.md` is
  now historical; the account is running the ETF strategy instead.
- **624+ tests pass.** Latest commit: see `git log -1`.

## What was built this session

- **Account scoping** — the login has two accounts and every ib_async read
  spanned both, so the funded one read $0.00. `ibkr/accounts.py` + 19 files
  fixed + a repo-wide regression test.
- **Streaming quotes** replacing `reqTickers()` (snapshot), plus freshness,
  spread, crossed-market and delayed-data gates in `read_quote()`.
- **Client Portal quote path** (`ibkr/cpapi.py`, `MEDIK_ETF_QUOTE_SOURCE=cpapi`)
  — the free feed works there but not through the TWS socket API.
- **Unattended running** — `run_medik_etf.bat`, file logging, TWS connect
  wait, holiday/half-day calendar, distinct exit codes.
- **Dry run** (`MEDIK_ETF_DRY_RUN=true`) — full pipeline on live quotes,
  stops exactly at `placeOrder`.

## Open items, in priority order

1. **Get the laptop current.** Nothing else is meaningful until the line
   count matches.
2. **Install IBKR Client Portal Gateway** on the laptop and log in at
   https://localhost:5000, then run with `MEDIK_ETF_QUOTE_SOURCE=cpapi`.
   Without it the cpapi source exits at preflight — correctly.
3. **The scheduled task never ran.** `MEDIK ETF AUTO TRADER`, Mon–Fri 06:45
   PT, had **`Triggers: Enabled = False`** and `LastRunTime 1999-11-30`. It
   must also point at `run_medik_etf.bat`, not at python.exe — Task Scheduler
   gives a fresh environment and `set` variables do not survive into it.
4. **The ETF backtest has never been run.** `fetch_data.bat` then
   `run_backtest.bat`. Historical data works on this account today, so this
   is unblocked and free. It is the only evidence about whether the strategy
   has an edge.
5. **Intraday momentum redesign** — I asked for it; a full spec (entry gates,
   0–100 score, exits, trailing, max hold, cooldown, caps) is in the
   conversation but NOT implemented. Read the COST ARITHMETIC section before
   building it.

## Standing rules (do not change without me saying so)

- `examples/medik_etf_live.py` is the ONE file allowed to submit live orders
  without per-order confirmation. Every other path keeps its human gate.
- No model output authorises a trade. `authorize_order()` is pure and
  deterministic; 19 gates, all must pass.
- Risk constants: 0.5% risk/trade, 1.0% ceiling, 1 position, 3 trades/session,
  2% daily loss, 90% capital utilisation, 15/30 min session buffers.
- Never trade delayed data. `read_quote()` refuses `marketDataType` 3 and 4.
- Kill switch: `C:\Users\Administrator\Medik\STOP_MEDIK`.

## How to talk to me

Tell me plainly when something will not work and why, with the number that
proves it. I have overridden your concerns before and may again — that is
fine, say it once and then build what I asked for.
