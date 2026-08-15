# `logs/` — background service logs

Written by `service/` when the Windows supervisor is running. **Contents are
gitignored** (see `.gitignore` here): the logs record account state, position
sizes, and connection details.

## Files

| file | written by | contents |
|---|---|---|
| `service.log` | `service/logging_setup.py` | Everything the supervisor does: connects, reconnects, health checks, cycle results. Rotates at **5 MB, 5 backups** (`service.log.1` … `.5`), so it is capped at ~30 MB total. |
| `alerts.log` | `service/alerts.py` | One line per critical failure, UTC-timestamped. **Append-only and never rotated** — it should stay small, and if it doesn't, that is itself the finding. |

## Why `alerts.log` exists separately

The service runs as a standalone Windows process with no Claude session, so
it **cannot** use the PushNotification tool that reaches the owner's phone —
that only exists inside a live session. Its fallbacks are a Windows toast
(only useful if someone is at the machine) and, optionally, SMTP email if the
`SMTP_*` variables are set in `.env`.

`alerts.log` is written **regardless of whether the toast succeeded**. It is
the durable record — the thing to read first when checking on an unattended
run, and the reason a missed alert is recoverable rather than lost.

## A quiet log is expected, not a bug

`SCAN_ENABLED` defaults to **false** (owner decision, 2026-08-07). With it
off, the supervisor still connects, reconnects, and health-checks, but never
runs a scan cycle in either mode. So a `service.log` full of connection and
health lines with no scan activity is the system working as configured.

The reason is in `CLAUDE.md`: the gate and pullback strategies both
backtested to negative expectancy, and the adopted strategy — QQQ
buy-and-hold — is deliberately inert. A running scanner would only generate
alerts for setups already decided against. Turn it on only for a strategy
re-validated through `backtest/net_of_commission.py`.

## Reading them

```powershell
Get-Content logs\service.log -Tail 50 -Wait     # live tail
Get-Content logs\alerts.log                     # every critical event, ever
Select-String -Path logs\service.log -Pattern 'ERROR|CRITICAL'
```

## Moving them

```
SERVICE_LOG_DIR=D:\medik-logs
```

Already documented in `.env.example`. Both `service/config.py` and
`paths.logs_dir()` read that one variable — there is deliberately no second
`MEDIK_LOGS_DIR`, because two names for one directory means whichever you
didn't set is the one that wins.

## What is *not* here

- **Trading decisions** — those go to the SQLite journal (`journal.sqlite` at
  the repo root, also gitignored). Read it with `examples/show_journal.py`.
- **Report output** — `reports/`.
- **Order history** — IBKR is the authority. The TWS Orders tab in particular
  is authoritative over the MCP connector, which has been observed
  under-reporting working orders.
