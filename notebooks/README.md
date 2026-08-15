# `notebooks/` — research and analysis

Ad-hoc exploration of the cached bars and backtest results. Unlike `data/`
and `reports/`, notebooks **are** tracked in git — they are source.

## Requirements

Jupyter is deliberately **not** in `requirements.txt`: the trading code, the
Windows service, and the backtests must install without dragging in a
notebook stack. Install it separately when you want it:

```bash
pip install -r notebooks/requirements.txt
jupyter lab notebooks/
```

## Contents

| notebook | what it does |
|---|---|
| `bar-cache-explorer.ipynb` | Loads a cache from `data/`, audits coverage and gaps, plots a symbol, and runs the buy-and-hold vs. 200-day-timing comparison on whatever is cached. |

## Rules of the road

**Commit notebooks with output cleared.** Executed cells embed account
figures and megabytes of base64 plot data in the diff. Before committing:

```bash
jupyter nbconvert --clear-output --inplace notebooks/*.ipynb
```

**A notebook result is not a validated strategy.** `CLAUDE.md` is explicit
that anything claimed to work must be re-run through
`backtest/net_of_commission.py` first — at this account's ~$35 position sizes
the commission floor costs ~2% per round trip, which is what turned a +1.57%
two-year gross result into −35.9% net. A gross equity curve in a notebook
that ignores that is not evidence of anything.

**Don't place orders from a notebook.** Execution policy is unchanged: Claude
drafts, the owner submits. `ibkr/orders.py` requires an explicit
`confirm=True`, and a notebook's re-run-the-cell-again ergonomics are a
uniquely bad fit for that.
