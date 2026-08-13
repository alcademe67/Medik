"""On-disk cache of daily OHLCV bars, stored as one JSON file per symbol.

Three backtest runners each carried their own copy of this loader, including
their own copy of the ragged-array check, all pointing at the same dead
scratchpad path. This is that logic, once.

File format (unchanged from the caches the fetch agents wrote, so existing
files load as-is):

    data/data2y/NVDA.json
    {"time": ["2024-08-05", ...], "open": [...], "high": [...],
     "low": [...], "close": [...], "volume": [...]}

The ragged-array check is not defensive boilerplate. The caches were written
by many agents fetching in parallel against a rate-limited connector, and
partial writes produced files whose arrays disagreed in length -- silently
mis-aligning prices against dates for a symbol, which a backtest will
happily trade on. CLAUDE.md lists the known-bad files (LUNR, REGN, TMO).
Length equality is the cheap invariant that catches it.

Completeness of the *last* bar is a separate concern and lives in
strategy.data_quality; pass it via `transform` rather than importing the
strategy layer here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

COLUMNS = ("open", "high", "low", "close", "volume")
_ALL_KEYS = ("time",) + COLUMNS


class CacheError(ValueError):
    """A cache file exists but cannot be trusted (missing keys, ragged arrays)."""


def cache_path(cache: Path, symbol: str) -> Path:
    return Path(cache) / f"{symbol.upper()}.json"


def cached_symbols(cache: Path) -> list[str]:
    """Symbols present in a cache directory. Empty list if it doesn't exist."""
    cache = Path(cache)
    if not cache.is_dir():
        return []
    return sorted(p.stem.upper() for p in cache.glob("*.json"))


def save_bars(df: pd.DataFrame, symbol: str, cache: Path) -> Path:
    """Write a bars frame (indexed by date, COLUMNS as columns) to the cache.

    Written to a temporary file and renamed, so an interrupted write leaves
    the previous file intact rather than a truncated one -- the failure mode
    that produced the ragged files in the first place.
    """
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise CacheError(f"{symbol}: frame is missing columns {missing}")

    payload = {"time": [pd.Timestamp(ts).strftime("%Y-%m-%d") for ts in df.index]}
    for col in COLUMNS:
        payload[col] = [None if pd.isna(v) else float(v) for v in df[col]]

    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    final = cache_path(cache, symbol)
    tmp = final.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(final)
    return final


def _frame_from_payload(symbol: str, raw: dict) -> pd.DataFrame:
    missing = [k for k in _ALL_KEYS if k not in raw]
    if missing:
        raise CacheError(f"{symbol}: cache file is missing keys {missing}")
    lengths = {k: len(raw[k]) for k in _ALL_KEYS}
    if len(set(lengths.values())) != 1:
        raise CacheError(f"{symbol}: ragged arrays {lengths} -- refetch this symbol")
    index = pd.to_datetime(raw["time"], utc=True).tz_localize(None)
    return pd.DataFrame({k: raw[k] for k in COLUMNS}, index=index).sort_index()


def load_bars(symbol: str, cache: Path) -> pd.DataFrame:
    """Load one symbol. Raises CacheError if the file is absent or ragged."""
    path = cache_path(cache, symbol)
    if not path.is_file():
        raise CacheError(f"{symbol}: no cache file at {path}")
    return _frame_from_payload(symbol.upper(), json.loads(path.read_text(encoding="utf-8")))


def load_cache(
    cache: Path,
    min_bars: int = 0,
    symbols: Iterable[str] | None = None,
    transform: Callable[[pd.DataFrame], pd.DataFrame] | None = None,
) -> tuple[dict[str, pd.DataFrame], list[tuple[str, str]]]:
    """Load a whole cache directory.

    Returns (frames_by_symbol, skipped) where skipped is a list of
    (symbol, reason). Bad symbols are skipped rather than raised so one
    corrupt file doesn't abort a 200-symbol backtest -- but they come back in
    `skipped` so the caller can report them instead of silently running on a
    smaller universe than it thinks.

    transform runs on each frame after loading (e.g. dropping an incomplete
    trailing bar) and before the min_bars check.
    """
    cache = Path(cache)
    frames: dict[str, pd.DataFrame] = {}
    skipped: list[tuple[str, str]] = []

    wanted = {s.upper() for s in symbols} if symbols is not None else None
    for symbol in cached_symbols(cache):
        if wanted is not None and symbol not in wanted:
            continue
        try:
            df = _frame_from_payload(
                symbol, json.loads(cache_path(cache, symbol).read_text(encoding="utf-8"))
            )
            if transform is not None:
                df = transform(df)
            if len(df) < min_bars:
                skipped.append((symbol, f"only {len(df)} bars, need {min_bars}"))
                continue
            frames[symbol] = df
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the sweep
            skipped.append((symbol, str(exc)[:80]))

    if wanted is not None:
        for symbol in sorted(wanted - set(frames) - {s for s, _ in skipped}):
            skipped.append((symbol, "not in cache"))
    return frames, skipped


def require_cache(cache: Path, **kwargs) -> tuple[dict[str, pd.DataFrame], list[tuple[str, str]]]:
    """load_cache, but exit with instructions instead of silently reporting
    zero trades when the cache is empty."""
    frames, skipped = load_cache(cache, **kwargs)
    if not frames:
        raise SystemExit(
            f"no usable bars in {cache}\n"
            f"  Populate it first:  python examples/fetch_bar_cache.py {Path(cache).name}\n"
            f"  (needs TWS open and logged in)"
        )
    return frames, skipped
