# Running the trading service on Windows boot

This wires `service/supervisor.py` to start automatically when your PC
boots, restart itself if it crashes, and log everything to rotating files.
It does **not** make IB Gateway log in without you — that limitation is
real and explained below, not glossed over.

## 1. IB Gateway: the part that's genuinely hard to fully automate

IB Gateway requires a login (username/password + 2FA) that IBKR does not
allow to be fully scripted for security reasons. Gateway's own **"Auto
Restart"** setting (Configuration > Lock and Exit) keeps an *already
logged-in* session alive across its mandatory daily restart, but it will
not perform the initial login after a cold Windows boot without you present.

Two honest options:

- **Manual**: log into IB Gateway once when you sit down at the machine
  each day/session. `supervisor.py`'s outer reconnect loop retries
  indefinitely with backoff, so it'll pick up the connection the moment
  Gateway is logged in — you don't have to time it.
- **IBC (IBAutomater / IBC)**: a well-known third-party open-source tool
  many IBKR API users use to script Gateway's login (it drives the Gateway
  UI programmatically, storing credentials locally). It's not part of this
  repo and I haven't installed or configured it for you — search "IBC
  Interactive Brokers" if you want to pursue it. Treat it as equivalent in
  sensitivity to storing your IBKR password on disk, because that's what it
  does.

Whichever you choose, `supervisor.py` doesn't need Gateway to be up *before*
it starts — it'll wait.

## 2. Configure `.env`

Copy `.env.example` to `.env` at the repo root if you haven't, and set:

```
MODE=PAPER          # or LIVE -- see service/config.py docstring for exactly
                     # what each does. LIVE never submits orders automatically.
IBKR_PORT=7497       # 7497 for paper, 7496 for live TWS, 4001/4002 for Gateway
POLL_INTERVAL_SECONDS=300
```

## 3. Create the Windows Task Scheduler task

1. Open **Task Scheduler** (search Start menu).
2. **Create Task...** (not "Basic Task" — you need the extra settings below).
3. **General** tab:
   - Name: `Medik Trading Service`
   - Check **"Run whether user is logged on or not"** if you want it to
     survive a logoff (you'll be prompted for your Windows password once).
4. **Triggers** tab → **New...**:
   - Begin the task: **At startup** (or **At log on** if you prefer it tied
     to your session).
5. **Actions** tab → **New...**:
   - Action: **Start a program**
   - Program/script: full path to `service\run_supervisor.bat`
   - Start in: the repo root folder
6. **Settings** tab — this is what gives you crash auto-restart:
   - Check **"If the task fails, restart every"**: `1 minute`
   - **"Attempt to restart up to"**: `999 times` (effectively indefinite)
   - Uncheck "Stop the task if it runs longer than..." (it's meant to run
     forever)
7. Save. Right-click the task → **Run** to test it immediately without
   rebooting.

## 4. Where to look when something's wrong

- `logs/service.log` — everything, rotating at 5MB x 5 files.
- `logs/alerts.log` — just the critical alerts (disconnects, health-check
  failures, mode mismatches, crashed cycles).
- A Windows toast notification pops up for each critical alert if you're
  logged in and at the machine (see `service/alerts.py` for exactly what
  can and can't reach you this way — it cannot push to your phone; that
  needs SMTP configured, see `.env.example`).

## 5. LIVE mode's actual output

In `MODE=LIVE`, the service queues qualifying setups into
`journal.sqlite`'s `pending_orders` table and alerts you — it never places
an order. Run this yourself, whenever you're ready to look:

```
python examples/review_pending_orders.py
```

Each queued setup is shown with a fresh live price and a re-check for
whether it's moved significantly since it was queued, and asks for an
explicit YES before placing anything.
