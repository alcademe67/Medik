# Restart prompt — paste this into a new Claude session to resume the trading system

I'm Ali, in Vancouver (Pacific time). We built a semi-automated trading system
for my Interactive Brokers account (TFSA, base currency USD) in the `Medik`
repository, on branch `claude/interactive-broker-python-connect-knsid8`.

**Read `CLAUDE.md` first** — it holds the standing risk rules, the execution
policy, verified account facts, and a list of environment gotchas that will
otherwise cost you hours (blocked domains, cron jobs being wiped, subagent
sleep hangs, IBKR rate limits). Then resume operations:

1. Verify the IBKR connector works: pull my account summary, positions, and
   open orders.
2. Check every position has a protective GTC stop resting. If anything is
   unprotected, tell me immediately with exact stop levels to place in the
   IBKR app (drafts appear under Portfolio > AI Instructions).
3. `CronList` — the scheduler wipes jobs constantly, so assume the watches
   are gone and re-arm them: the weekday 6:47 AM Pacific morning scan over
   `strategy/universe.py` (~204 names) through **both** strategies via
   `<scratchpad>/eval_both.py`, and the intraday breakout watch.
4. Execution policy (do not change): you never place, modify, or cancel live
   orders yourself. You draft, I tap. Buys always come with stop + target
   levels. Fully hands-free trading is allowed only on my paper account
   (`examples/autotrade_paper.py`, or `service/` with `MODE=PAPER`).

## Where things stood at the last handoff (2026-08-05)

- **Positions:** F (9 sh @ ~14.45) and JPM (0.09 sh @ ~351.85), both with
  resting GTC stops (13.70 / 332.55). Net liq ~$300, ~54% deployed.
- **Built and pushed:** scoring engine, SQLite decision journal, data-quality
  validation, full risk engine (1% risk cap, drawdown circuit breakers,
  trailing/breakeven/partial-profit), trend-pullback strategy, ~204-name
  universe, and a Windows background service under `service/`.
- **Open item — the important one:** the trend-pullback strategy is
  **UNVALIDATED**. A 2-year dataset (~193 of 204 names) is cached at
  `<scratchpad>/data2y/*.json` specifically to backtest it. Run that before
  trusting pullback signals. See rule 8 in `CLAUDE.md`, including the
  provenance caveat about where those rules actually came from.
- **Open item:** deploying `service/` on my Windows machine never got past
  step one — I still need to run `python examples\connect_test.py` there and
  send you the output. See `service/SETUP_WINDOWS.md`.
- **Open item:** my second IBKR username (`alcademe6767`) is still pending
  identity verification, so TWS and my phone app keep kicking each other off.

Known contract IDs: NVDA 4815747, XOM 895178251, CVX 5684, RIOT 292830677,
MARA 474219659, NFLX 15124833, PLUG 88385302, JPM 1520593, F 9599491,
SNDK 760250490, AMD 4391, PLTR 444857009, SOFI 494162724, HIMS 466188387,
RKLB 787273575, IONQ 517593749, ASTS 480745767, TEM 709237125.
