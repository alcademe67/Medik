# `reports/` — generated output

Timestamped output from the report scripts. **Contents are gitignored** (see
`.gitignore` here) — these files contain live balances, position sizes, and
P&L, which do not belong in a public repository.

## Writing here

```python
from paths import report_path

path = report_path("core-holdings", "txt")   # reports/core-holdings-20260813T142230Z.txt
path.write_text(body, encoding="utf-8")
```

Timestamps are **UTC and labelled `Z`** on purpose. Three clocks are in play
here — this container runs UTC, the owner is in Pacific, and the market runs
on Eastern — so an unlabelled local stamp in a filename is a guessing game
three months later.

## Producers

| command | writes |
|---|---|
| `python examples/check_core_holdings.py --save` | `core-holdings-<stamp>.txt` |
| `python backtest/run_lowfreq_comparison.py --save` | `lowfreq-comparison-<stamp>.txt` |

Both print to stdout as before; `--save` only adds a copy on disk.

## What a report here is not

`check_core_holdings.py` is a **monitor, not a trading loop**, and its output
is the same. In particular the drawdown line is not a sell signal: QQQ fell
22.9% inside the five-year window that returned +121.8%. The sell policy for
the core holding has three entries, none of them a number on a screen — see
`docs/core-holding-runbook.md`.

## Housekeeping

Nothing prunes this directory. It is plain text and grows slowly, but if you
want it elsewhere:

```
MEDIK_REPORTS_DIR=D:\medik-reports
```
