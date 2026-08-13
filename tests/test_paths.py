"""Tests for paths.py — the resolver for data/, reports/ and notebooks/.

Run: python -m pytest tests/test_paths.py -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import paths


# --- defaults -------------------------------------------------------------

def test_defaults_are_inside_the_repo(monkeypatch):
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.delenv(paths.REPORTS_DIR_ENV, raising=False)
    assert paths.data_dir() == paths.REPO_ROOT / "data"
    assert paths.notebooks_dir() == paths.REPO_ROOT / "notebooks"


def test_data_dir_does_not_create_by_default(tmp_path, monkeypatch):
    """A missing cache must surface as "no data, go fetch it" rather than as
    a silently-created empty folder a backtest then reports zero trades from."""
    target = tmp_path / "nope"
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(target))
    assert paths.data_dir() == target
    assert not target.exists()

    assert paths.data_dir(create=True) == target
    assert target.is_dir()


# --- environment overrides ------------------------------------------------

def test_env_overrides_win(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "d"))
    monkeypatch.setenv(paths.REPORTS_DIR_ENV, str(tmp_path / "r"))
    assert paths.data_dir() == tmp_path / "d"
    assert paths.reports_dir() == tmp_path / "r"


def test_blank_env_falls_back_to_default(monkeypatch):
    """An empty or whitespace-only value is a typo, not a request to use the
    current working directory."""
    monkeypatch.setenv(paths.DATA_DIR_ENV, "   ")
    assert paths.data_dir() == paths.REPO_ROOT / "data"


def test_notebooks_dir_is_not_overridable(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
    assert paths.notebooks_dir() == paths.REPO_ROOT / "notebooks"


# --- bar caches -----------------------------------------------------------

def test_bar_cache_dir_nests_under_data(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
    assert paths.bar_cache_dir("data2y") == tmp_path / "data2y"


def test_bar_cache_dir_creates_the_whole_chain(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "outer"))
    made = paths.bar_cache_dir("data5y", create=True)
    assert made.is_dir()


@pytest.mark.parametrize("bad", ["", "..", ".", "a/b", "a\\b", "../escape"])
def test_bar_cache_dir_rejects_traversal(bad, tmp_path, monkeypatch):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
    with pytest.raises(ValueError):
        paths.bar_cache_dir(bad)


# --- report paths ---------------------------------------------------------

def test_report_path_is_timestamped_and_utc(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.REPORTS_DIR_ENV, str(tmp_path))
    when = datetime(2026, 8, 13, 14, 22, 30, tzinfo=timezone.utc)
    assert paths.report_path("core-holdings", "txt", when).name == \
        "core-holdings-20260813T142230Z.txt"


def test_report_path_converts_other_zones_to_utc(tmp_path, monkeypatch):
    """Three clocks are in play here (container UTC, owner Pacific, market
    Eastern), so the stamp must be normalized, not whatever it was handed."""
    monkeypatch.setenv(paths.REPORTS_DIR_ENV, str(tmp_path))
    pacific = datetime(2026, 8, 13, 7, 22, 30, tzinfo=timezone(timedelta(hours=-7)))
    assert paths.report_path("r", "txt", pacific).name == "r-20260813T142230Z.txt"


def test_report_path_tolerates_a_dotted_extension(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.REPORTS_DIR_ENV, str(tmp_path))
    assert paths.report_path("r", ".csv").suffix == ".csv"


def test_report_path_creates_the_directory(tmp_path, monkeypatch):
    target = tmp_path / "fresh"
    monkeypatch.setenv(paths.REPORTS_DIR_ENV, str(target))
    paths.report_path("r")
    assert target.is_dir()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
