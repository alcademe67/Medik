"""Filesystem locations for the repo's non-code directories.

Every runnable script here already puts the repo root on sys.path, so
`import paths` resolves to this file from anywhere in the tree.

Why this exists: the backtest runners used to hardcode absolute paths into
an agent scratchpad (/tmp/claude-0/.../scratchpad/data2y). Scratchpads are
per-session and are deleted with the container, so those paths pointed at
nothing and the backtests behind the adopted strategy could not be re-run.
Caches and generated reports now live in the working tree, next to the code
that reads them.

Both locations can be moved with an environment variable, which matters on
the owner's Windows machine if the repo lives on a small drive:

    MEDIK_DATA_DIR=D:\\medik-data
    MEDIK_REPORTS_DIR=D:\\medik-reports
    SERVICE_LOG_DIR=D:\\medik-logs
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

DATA_DIR_ENV = "MEDIK_DATA_DIR"
REPORTS_DIR_ENV = "MEDIK_REPORTS_DIR"
# Deliberately NOT "MEDIK_LOGS_DIR": the service has read SERVICE_LOG_DIR
# since it was written, it is documented in .env.example, and it may already
# be set on the owner's machine. Two environment variables naming the same
# directory is a trap -- whichever one you didn't set is the one that wins.
LOGS_DIR_ENV = "SERVICE_LOG_DIR"


def _resolve(env_var: str, default_name: str, create: bool) -> Path:
    override = os.environ.get(env_var, "").strip()
    path = Path(override).expanduser() if override else REPO_ROOT / default_name
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir(create: bool = False) -> Path:
    """Root of the price-data caches (`data/`, or $MEDIK_DATA_DIR).

    Defaults to create=False: a missing cache directory should surface as a
    "no data, go fetch it" error, not as a silently-created empty folder
    that a backtest then reports zero trades from.
    """
    return _resolve(DATA_DIR_ENV, "data", create)


def reports_dir(create: bool = True) -> Path:
    """Root for generated reports (`reports/`, or $MEDIK_REPORTS_DIR).

    Defaults to create=True — the caller is about to write into it.
    """
    return _resolve(REPORTS_DIR_ENV, "reports", create)


def logs_dir(create: bool = False) -> Path:
    """`logs/`, or $SERVICE_LOG_DIR — rotating service logs and alerts.log.

    Defaults to create=False: the two writers (service.logging_setup and
    service.alerts) each mkdir before opening a handle, and a *reader*
    looking for logs that don't exist should see that, not an empty
    directory this call conjured.
    """
    return _resolve(LOGS_DIR_ENV, "logs", create)


def notebooks_dir() -> Path:
    """`notebooks/` — tracked in git, so it is never env-overridable."""
    return REPO_ROOT / "notebooks"


def bar_cache_dir(name: str, create: bool = False) -> Path:
    """A named bar cache under data/ — e.g. bar_cache_dir("data2y").

    The name is a single directory component by convention (the caches are
    named for their span: data1y, data2y, data5y).
    """
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"bar cache name must be a single directory component, got {name!r}")
    path = data_dir(create=create) / name
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def report_path(stem: str, ext: str = "txt", when: datetime | None = None) -> Path:
    """A timestamped path under reports/, e.g. core-holdings-20260813T142230Z.txt

    The stamp is UTC and says so, because neither clock in play here is the
    obvious one: this container runs UTC, the owner is in Pacific, and the
    market runs on Eastern. An unlabelled local timestamp in a filename is a
    guessing game three months later.
    """
    when = when or datetime.now(timezone.utc)
    stamp = when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ext = ext.lstrip(".")
    return reports_dir(create=True) / f"{stem}-{stamp}.{ext}"
