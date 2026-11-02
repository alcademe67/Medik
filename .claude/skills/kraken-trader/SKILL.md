---
name: kraken-trader
description: >-
  The owner's Kraken (crypto spot) account: the Kraken counterpart of the
  eleven trader-* stock skills. Scans the most-traded Kraken pairs for
  breakouts / oversold / trend setups, times an entry on completed bars,
  plans a stop and a fee-aware target, replays the plan (10,000 simulated
  paths plus every past time the rule fired, NET of Kraken fees), sizes it
  to the account's risk limits, risk-checks it PASS/BLOCK, shows the ticket,
  records the owner's yes, and hands the owner a command that submits it
  only after Kraken's validate-only preview and a typed YES. Also: quotes,
  balances, open orders, alerts on stop/target, flatten (sell a holding),
  cancel, and a daily status. Use when the user says "Kraken", "crypto",
  "bitcoin", "BTC", "ETH", "SOL", "scan crypto", "buy/sell <coin>", "my
  Kraken balance", "check my crypto", or "follow up on my Kraken trade".
argument-hint: "[scan | quote PAIR | entry PAIR | plan | replay | size | risk | ticket | status ...]"
---

# kraken-trader

One script, `${CLAUDE_SKILL_DIR}/kraken.py` (standard-library Python 3.8+),
covering the same jobs as the eleven stock skills, rebuilt for crypto: 24/7
markets (no opening range, no "flat by the close"), fractional coin volumes,
Kraken's own minimums and its fee tiers. State goes to `trader/kraken/`
(gitignored: it holds balances). On Windows use `python`, elsewhere `python3`.

## The eleven jobs, on Kraken

| stock skill | Kraken command | notes |
| --- | --- | --- |
| trader-scanner | `kraken.py scan [--top 30 --interval 60 --lookback 20]` | top pairs by 24h volume in your quote currency |
| trader-news | WebSearch | "<coin> news today"; there are no 8-Ks for coins |
| trader-entry | `kraken.py entry PAIR [--interval 60 --lookback 20]` | first completed bar to CLOSE above the prior range high |
| trader-exit | `kraken.py plan [--stop range-mid\|range-low\|pct:N\|atr:K\|PRICE] [--r 2] [--horizon 24]` | target set so reward:risk is met NET of both fees at the worst-case fill |
| trader-timemachine | `kraken.py replay [--n 10000 --seed 19]` | ALWAYS run it; quote its verdict verbatim |
| trader-budget | `kraken.py size` | refuses rather than shrinks below Kraken's minimum |
| trader-risk | `kraken.py risk` | PASS/BLOCK; exit 1 if anything blocked |
| trader-broker | `kraken.py ticket`, `approve`, then the OWNER runs `place` | see "Placing an order" |
| trader-alerts | `kraken.py alerts`, `kraken.py quote PAIR` | stop / target / distance |
| trader-analyst | n/a | coins have no earnings reports |
| trader-pelosi | `/trader-pelosi` as-is | not account-specific |

Plus: `status` (account, today's risk, plan, last verdict), `orders` (open
orders on Kraken), `flatten PAIR` (sell what is held, limit under the bid),
`cancel TXID` (owner, typed YES).

## Setup (the owner does this once, on their own computer)

1. Kraken → Settings → API → create a key with ONLY: query funds, query open
   and closed orders/trades, create & modify orders, cancel/close orders.
   **Never enable withdrawals.** Leave margin/futures off.
2. Put the key in the terminal's environment, never in the chat:
   PowerShell `$env:KRAKEN_API_KEY="..."; $env:KRAKEN_API_SECRET="..."`.
   If the owner pastes a key into the chat: tell them to delete the message
   and revoke that key in Kraken. Never ask for, print, or store keys.
3. Optional `trader/kraken/config.json` to change limits (defaults below).
   Without keys, set `account_value` and `cash` there to plan and size offline.

Defaults mirror the owner's IBKR rules: 1% of account value lost at the stop,
20% per coin, 80% deployed max / 20% cash floor, reward:risk >= 2 net of fees,
3% daily loss, 3 entries a day (UTC day), buy limits 0.3% above the entry.
Never loosen a limit on the owner's behalf; they edit the config themselves.

## The flow for one trade

`scan` → news (WebSearch) → `entry PAIR` → `plan` → `replay` → `size` →
`risk` → `ticket` → owner says yes → `approve` → owner runs `place` →
`alerts` while open → `flatten` to take the target.

**Say the replay verdict out loud before sizing.** If it says NEGATIVE after
fees, say so plainly and recommend not trading it; if the owner still wants
to, that is their call, and every other gate still applies.

## Placing an order (three gates, every order)

1. `risk` PASSed this exact order today (matched by fingerprint; any edit
   needs a fresh PASS).
2. Show ONE ticket, ask "Place this order? Reply yes or no." Then
   `kraken.py approve --index N --reply "<their reply, word for word>"`. If
   it prints "Not recorded", ask again. Never turn a reply into a yes. Never
   offer "yes to all". A yes expires after 15 minutes.
3. Give the owner the `place` command it prints. **The owner runs it in their
   own terminal**: it asks Kraken to validate (nothing submitted), shows the
   result, and submits only when they type `YES`. It refuses to run from a
   non-interactive shell, so Claude cannot submit it, by design. Do not
   work around this (no piping YES, no pty tricks).

After a fill, check with `orders` / `status`. The entry carries an attached
stop-loss (Kraken's conditional close). The target is not attached; when
`alerts` says TARGET, offer `flatten` (which goes through risk/ticket/yes/
place again). Cancel the attached stop before flattening, or Kraken will
see the coins as already committed.

## Following up

- At the start of any session that mentions Kraken, run `status`, and run
  `alerts` if there is an open plan.
- To watch an open trade, if Claude Code runs on the owner's machine: `/loop 15m`
  running `kraken.py alerts`. That is read-only. Never place or approve
  anything from a loop or a scheduled task: nobody is there to say yes.
- Cloud sessions cannot reach api.kraken.com (network policy). There, ask
  the owner to paste `status` / `alerts` output, or plan offline from config.

## Rules

- Spot, long-only. No margin, leverage, futures or shorting.
- Kraken spot has **no paper account**: every placed order is real money.
- Crypto is not TFSA-eligible: this is a taxable account, every sale is a
  disposition, and frequent trading can be treated as business income by
  the CRA. Not tax advice.
- Not financial advice. A PASS is a rules check, not a recommendation.
- Only completed bars feed signals (the forming bar is dropped). Kraken
  serves at most 720 bars per interval, so replays cover a short window.
