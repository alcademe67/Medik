# `docs/`

Operating documentation. Two files stay at the repo root on purpose:

- **`CLAUDE.md`** — the standing risk rules, execution policy, account facts,
  and hard-won environment gotchas. It is the authority; anything here that
  contradicts it is wrong. Read it first.
- **`README.md`** — first-run setup (TWS configuration, dependencies,
  connection test).

## Contents

| file | what it covers |
|---|---|
| [`RESTART_PROMPT.md`](RESTART_PROMPT.md) | Paste-into-a-new-session handoff: where things stood, what to verify first, open items. |
| [`SETUP_WINDOWS.md`](SETUP_WINDOWS.md) | Installing `service/` as a Windows background service via Task Scheduler. |
| [`core-holding-runbook.md`](core-holding-runbook.md) | The live QQQ position: how to check it, how to add to it, when to sell it, and the order-entry mistakes that have actually cost money here. |
| [`backtest-verdict.md`](backtest-verdict.md) | What was tested, what the numbers were, what the caveats are, and how to reproduce it now that the data cache is in the tree. |
| [`mcp-servers.md`](mcp-servers.md) | Registering additional MCP servers (Quiver Quantitative, trader-dev) with Claude Code. |

## Directories these docs refer to

| directory | tracked in git? | what's in it |
|---|---|---|
| `data/` | README only | Cached daily bars from IBKR. Regenerable via `examples/fetch_bar_cache.py`. |
| `reports/` | README only | Timestamped script output. Contains live balances, so it stays out of git. |
| `notebooks/` | yes | Research notebooks. Commit them with output cleared. |

Each has its own README with the details.
