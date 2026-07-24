# Restart prompt — paste this into a new Claude session to resume the trading system

I'm Ali, in Vancouver (Pacific time). We built a semi-automated trading system
for my Interactive Brokers account (TFSA, base currency USD) in the `Medik`
repository. Read `CLAUDE.md` for the standing rules and resume operations:

1. Verify your IBKR connector works: pull my account summary, positions, and
   open orders.
2. Check every position has a protective GTC stop order resting. If anything
   is unprotected, tell me immediately with exact stop levels to place in the
   IBKR app (my drafts appear under Portfolio > AI Instructions).
3. Re-arm the weekday morning scan at 6:47 AM Pacific (9:47 AM ET): scan the
   24-name universe through the 4-indicator gate on completed daily bars
   (strategy code is in `strategy/`, evaluator pattern in the scratchpad or
   `examples/run_scan.py`), news-veto candidates, size trades at max 20% of
   available funds with max 80% of equity deployed and 1:3 risk/reward, then
   create order drafts and send ONE push notification to my phone.
4. Execution policy (do not change): you never place, modify, or cancel live
   orders yourself. You draft, I tap. Buys always come with stop + target
   levels. Fully hands-free trading is allowed only on my paper account via
   `examples/autotrade_paper.py`.
5. My phone approves drafts in the IBKR app; my TWS on the desktop is only
   for running the repo scripts myself (e.g. `examples/protect_positions.py`).

Known facts: contract IDs — NVDA 4815747, XOM 895178251, RIOT 292830677,
MARA 474219659, NFLX 15124833, PLUG 88385302. My usernames: alcademe67
(TWS/desktop), alcademe6767 (pending approval, will be phone). One login per
username — apps kick each other off until the second username activates.
