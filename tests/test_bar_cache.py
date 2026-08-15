"""Tests for ibkr/cache.py — the on-disk daily-bar cache.

The invariant that matters most here is length equality across time + the
five OHLCV arrays. The original caches were written by many agents fetching
in parallel against a rate-limited connector, and partial writes produced
files whose arrays disagreed in length. That silently mis-aligns prices
against dates, and a backtest will trade on it without complaining.

Run: python -m pytest tests/test_bar_cache.py -q
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

from ibkr.cache import (
    CacheError,
    cache_path,
    cached_symbols,
    load_bars,
    load_cache,
    require_cache,
    save_bars,
)


def make_frame(n: int = 5, start: str = "2026-01-01") -> pd.DataFrame:
    index = pd.date_range(start, periods=n, freq="D")
    return pd.DataFrame(
        {
            "open": [10.0 + i for i in range(n)],
            "high": [11.0 + i for i in range(n)],
            "low": [9.0 + i for i in range(n)],
            "close": [10.5 + i for i in range(n)],
            "volume": [1000.0 + i for i in range(n)],
        },
        index=index,
    )


def write_raw(cache, symbol: str, payload: dict) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    cache_path(cache, symbol).write_text(json.dumps(payload), encoding="utf-8")


# --- round trip -----------------------------------------------------------

def test_save_then_load_round_trips(tmp_path):
    original = make_frame()
    save_bars(original, "NVDA", tmp_path)
    loaded = load_bars("NVDA", tmp_path)

    assert list(loaded.columns) == ["open", "high", "low", "close", "volume"]
    assert len(loaded) == len(original)
    assert loaded["close"].tolist() == original["close"].tolist()
    assert loaded.index[0] == original.index[0]


def test_symbols_are_normalized_to_uppercase(tmp_path):
    save_bars(make_frame(), "nvda", tmp_path)
    assert cache_path(tmp_path, "nvda").name == "NVDA.json"
    assert cached_symbols(tmp_path) == ["NVDA"]
    assert len(load_bars("nvda", tmp_path)) == 5


def test_save_rejects_a_frame_missing_columns(tmp_path):
    with pytest.raises(CacheError, match="missing columns"):
        save_bars(make_frame().drop(columns=["volume"]), "NVDA", tmp_path)


def test_save_leaves_no_temp_file_behind(tmp_path):
    save_bars(make_frame(), "NVDA", tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["NVDA.json"]


def test_cached_symbols_on_a_missing_directory(tmp_path):
    assert cached_symbols(tmp_path / "nope") == []


# --- the ragged-array invariant -------------------------------------------

def test_ragged_arrays_are_rejected(tmp_path):
    """The LUNR/REGN/TMO failure mode: a short array silently mis-aligns
    prices against dates."""
    write_raw(tmp_path, "LUNR", {
        "time": ["2026-01-01", "2026-01-02", "2026-01-03"],
        "open": [1.0, 2.0, 3.0],
        "high": [1.0, 2.0, 3.0],
        "low": [1.0, 2.0, 3.0],
        "close": [1.0, 2.0],          # one short
        "volume": [1.0, 2.0, 3.0],
    })
    with pytest.raises(CacheError, match="ragged"):
        load_bars("LUNR", tmp_path)


def test_missing_keys_are_rejected(tmp_path):
    write_raw(tmp_path, "X", {"time": ["2026-01-01"], "close": [1.0]})
    with pytest.raises(CacheError, match="missing keys"):
        load_bars("X", tmp_path)


def test_absent_file_is_a_cache_error(tmp_path):
    with pytest.raises(CacheError, match="no cache file"):
        load_bars("NOPE", tmp_path)


def test_bars_are_sorted_by_date(tmp_path):
    write_raw(tmp_path, "X", {
        "time": ["2026-01-03", "2026-01-01", "2026-01-02"],
        "open": [3.0, 1.0, 2.0], "high": [3.0, 1.0, 2.0], "low": [3.0, 1.0, 2.0],
        "close": [3.0, 1.0, 2.0], "volume": [3.0, 1.0, 2.0],
    })
    assert load_bars("X", tmp_path)["close"].tolist() == [1.0, 2.0, 3.0]


# --- load_cache -----------------------------------------------------------

def test_one_bad_file_does_not_abort_the_sweep(tmp_path):
    """A 200-symbol backtest must not die on one corrupt file -- but the
    caller has to be able to see that it happened."""
    save_bars(make_frame(), "AAPL", tmp_path)
    save_bars(make_frame(), "MSFT", tmp_path)
    write_raw(tmp_path, "TMO", {"time": ["2026-01-01"], "open": [1.0], "high": [1.0],
                                "low": [1.0], "close": [], "volume": [1.0]})

    frames, skipped = load_cache(tmp_path)
    assert sorted(frames) == ["AAPL", "MSFT"]
    assert [s for s, _ in skipped] == ["TMO"]
    assert "ragged" in skipped[0][1]


def test_min_bars_filters_short_history(tmp_path):
    """SNDK/WOLF have genuinely short history from a spinoff -- correct data,
    just not enough of it."""
    save_bars(make_frame(300), "AAPL", tmp_path)
    save_bars(make_frame(10), "SNDK", tmp_path)

    frames, skipped = load_cache(tmp_path, min_bars=250)
    assert sorted(frames) == ["AAPL"]
    assert skipped == [("SNDK", "only 10 bars, need 250")]


def test_transform_runs_before_the_min_bars_check(tmp_path):
    save_bars(make_frame(10), "AAPL", tmp_path)
    frames, skipped = load_cache(tmp_path, min_bars=10, transform=lambda df: df.iloc[:-1])
    assert frames == {}
    assert skipped == [("AAPL", "only 9 bars, need 10")]


def test_symbols_filter_reports_what_was_absent(tmp_path):
    save_bars(make_frame(), "AAPL", tmp_path)
    frames, skipped = load_cache(tmp_path, symbols=["AAPL", "TSLA"])
    assert sorted(frames) == ["AAPL"]
    assert skipped == [("TSLA", "not in cache")]


def test_symbols_filter_is_case_insensitive(tmp_path):
    save_bars(make_frame(), "AAPL", tmp_path)
    frames, _ = load_cache(tmp_path, symbols=["aapl"])
    assert sorted(frames) == ["AAPL"]


# --- require_cache --------------------------------------------------------

def test_require_cache_exits_with_instructions_when_empty(tmp_path):
    """Better than reporting zero trades from an empty universe, which reads
    as "the strategy found nothing" rather than "there was no data"."""
    with pytest.raises(SystemExit) as exc:
        require_cache(tmp_path / "data2y")
    assert "fetch_bar_cache.py data2y" in str(exc.value)


def test_require_cache_passes_through_when_populated(tmp_path):
    save_bars(make_frame(), "AAPL", tmp_path)
    frames, skipped = require_cache(tmp_path)
    assert sorted(frames) == ["AAPL"]
    assert skipped == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
