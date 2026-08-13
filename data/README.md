# `data/` — cached daily bars

Local cache of daily OHLCV bars pulled from IBKR. **Contents are gitignored**
(see `.gitignore` here): the files are large, regenerable, and tied to the
account's market-data permissions.

## Why this folder exists

The backtest runners used to read from an agent scratchpad:

```
/tmp/claude-0/-home-user-Medik/6e4758d5-.../scratchpad/data2y
```

Scratchpads are per-session and are deleted with the container, so that path
had pointed at nothing for some time. The practical consequence: the
backtests behind the **adopted QQQ buy-and-hold strategy** — the ones
`CLAUDE.md` says must be re-run before any strategy change is called working
— could not be re-run at all. The cache now lives in the working tree, and
`paths.data_dir()` resolves it.

## Layout

One directory per cache span, one JSON file per symbol:

```
data/
  data2y/NVDA.json     ~2 years of daily bars, ~200 stock symbols
  data5y/QQQ.json      ~5 years, the ETFs used by backtest/lowfreq.py
```

File format (unchanged from the original caches, so old files load as-is):

```json
{"time": ["2024-08-05", ...], "open": [...], "high": [...],
 "low": [...], "close": [...], "volume": [...]}
```

## Filling it

```bash
python examples/fetch_bar_cache.py data2y --universe          # ~204 names, 2y
python examples/fetch_bar_cache.py data5y --etfs --duration "5 Y"
python examples/fetch_bar_cache.py data2y NVDA AMD PLTR       # specific symbols
```

Needs TWS open and logged in. It is resumable — symbols already cached are
skipped unless you pass `--refetch`, which matters because a full universe
fetch is slow and the connector rate-limits.

## Reading it

Use `ibkr.cache`, not a hand-rolled loader:

```python
from ibkr.cache import load_cache
from paths import bar_cache_dir

frames, skipped = load_cache(bar_cache_dir("data2y"), min_bars=250)
```

`load_cache` skips bad files and returns them in `skipped` rather than
raising, so one corrupt symbol doesn't abort a 200-symbol run — but check
`skipped` and report it, or you will quietly backtest a smaller universe
than you think you did.

## The ragged-array check, and why it is not paranoia

The original caches were written by many agents fetching in parallel against
a rate-limited connector. Partial writes produced files whose arrays
disagreed in length, which silently mis-aligns prices against dates — a
backtest will trade on that without complaining. `ibkr.cache` refuses any
file where `time` and the five OHLCV arrays aren't all the same length, and
`save_bars` writes to a temp file and renames, so an interrupted write can't
produce one.

Known-bad in the original 2y cache (refetch before use): **LUNR, REGN, TMO**
(ragged). **CFLT, BITF** have no resolvable US listing. **SNDK, WOLF** have
genuinely short history from a spinoff/reorg — those are correct, not
corrupt, and `min_bars` will filter them.

## Completeness of the last bar

Loading is separate from `strategy.data_quality.drop_incomplete_trailing_bar`,
which drops a trailing bar whose US session hasn't closed yet. Pass it in:

```python
from strategy.data_quality import drop_incomplete_trailing_bar

frames, skipped = load_cache(
    bar_cache_dir("data2y"),
    transform=lambda df: drop_incomplete_trailing_bar(df)[0],
)
```

A mid-session bar cached and reused after the close is what produced a false
gate failure on 2026-08-03. Don't skip it.

## Moving it off the repo drive

```
MEDIK_DATA_DIR=D:\medik-data
```

`paths.data_dir()` honours it everywhere.
