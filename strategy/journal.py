"""SQLite trade/decision journal.

Every scan run, every candidate evaluated (pass or fail, with its indicator
checks and score), every decision made about it (drafted / news-vetoed /
sizing-rejected / skipped because it gapped), and every closed trade gets a
durable, queryable record here -- so "why did/didn't we trade X on that day"
has an answer in the database, not just buried in a chat transcript.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

DEFAULT_DB_PATH = os.environ.get(
    "JOURNAL_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "journal.sqlite"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_time TEXT NOT NULL,
    universe_size INTEGER NOT NULL,
    available_funds REAL,
    net_liquidation REAL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER REFERENCES scans(id),
    symbol TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    side TEXT,
    passed INTEGER NOT NULL,
    score REAL,
    entry REAL,
    stop REAL,
    target REAL,
    checks_json TEXT,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER REFERENCES candidates(id),
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at TEXT NOT NULL,
    net_liquidation REAL NOT NULL
);

-- LIVE-mode candidates that passed the gate, score threshold, sizing, and
-- portfolio risk limits land here -- NOT as a live order. status starts
-- "pending" and only changes when a human runs
-- examples/review_pending_orders.py and explicitly acts on each row.
CREATE TABLE IF NOT EXISTS pending_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queued_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    target REAL NOT NULL,
    score REAL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | placed | skipped | expired
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_date TEXT,
    entry_price REAL,
    exit_date TEXT,
    exit_price REAL,
    exit_reason TEXT,
    quantity REAL,
    stop REAL,
    target REAL,
    capital_at_risk REAL,
    realized_pnl REAL,
    r_multiple REAL,
    notes TEXT
);
"""

# decisions.action values: drafted | news_vetoed | sizing_rejected |
# headroom_rejected | gapped_skip | manual_skip


@contextmanager
def _connect(db_path: str = DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)


def log_scan(
    run_time: str,
    universe_size: int,
    available_funds: float | None = None,
    net_liquidation: float | None = None,
    notes: str = "",
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO scans (run_time, universe_size, available_funds, net_liquidation, notes) "
            "VALUES (?,?,?,?,?)",
            (run_time, universe_size, available_funds, net_liquidation, notes),
        )
        return cur.lastrowid


def log_candidate(
    scan_id: int,
    symbol: str,
    checked_at: str,
    side: str | None,
    passed: bool,
    score: float | None = None,
    entry: float | None = None,
    stop: float | None = None,
    target: float | None = None,
    checks: dict | None = None,
    reason: str = "",
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO candidates
               (scan_id, symbol, checked_at, side, passed, score, entry, stop, target, checks_json, reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                scan_id, symbol, checked_at, side, int(passed), score, entry, stop, target,
                json.dumps(checks) if checks else None, reason,
            ),
        )
        return cur.lastrowid


def log_decision(
    symbol: str,
    action: str,
    created_at: str,
    candidate_id: int | None = None,
    detail: str = "",
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO decisions (candidate_id, symbol, action, detail, created_at) VALUES (?,?,?,?,?)",
            (candidate_id, symbol, action, detail, created_at),
        )
        return cur.lastrowid


def log_trade(
    symbol: str,
    side: str,
    entry_date: str | None = None,
    entry_price: float | None = None,
    exit_date: str | None = None,
    exit_price: float | None = None,
    exit_reason: str = "",
    quantity: float | None = None,
    stop: float | None = None,
    target: float | None = None,
    capital_at_risk: float | None = None,
    realized_pnl: float | None = None,
    r_multiple: float | None = None,
    notes: str = "",
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (symbol, side, entry_date, entry_price, exit_date, exit_price, exit_reason,
                quantity, stop, target, capital_at_risk, realized_pnl, r_multiple, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                symbol, side, entry_date, entry_price, exit_date, exit_price, exit_reason,
                quantity, stop, target, capital_at_risk, realized_pnl, r_multiple, notes,
            ),
        )
        return cur.lastrowid


def log_equity_snapshot(net_liquidation: float, taken_at: str, db_path: str = DEFAULT_DB_PATH) -> int:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO equity_snapshots (taken_at, net_liquidation) VALUES (?,?)",
            (taken_at, net_liquidation),
        )
        return cur.lastrowid


def equity_baselines(now: datetime | None = None, db_path: str = DEFAULT_DB_PATH) -> dict:
    """Returns {"day": ..., "week": ..., "month": ...}, each the net
    liquidation of the EARLIEST equity snapshot recorded within the current
    UTC day / ISO week (Mon-start) / calendar month, or None if no snapshot
    has been logged yet for that period -- callers should skip that
    drawdown check rather than treat 0.0/None as a real baseline.
    """
    now = now or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    month_start = day_start.replace(day=1)

    init_db(db_path)
    out = {}
    with _connect(db_path) as conn:
        for key, since in (("day", day_start), ("week", week_start), ("month", month_start)):
            row = conn.execute(
                "SELECT net_liquidation FROM equity_snapshots WHERE taken_at >= ? "
                "ORDER BY taken_at ASC LIMIT 1",
                (since.isoformat(),),
            ).fetchone()
            out[key] = row[0] if row else None
    return out


def queue_pending_order(
    symbol: str,
    side: str,
    quantity: float,
    entry: float,
    stop: float,
    target: float,
    queued_at: str,
    score: float | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO pending_orders
               (queued_at, symbol, side, quantity, entry, stop, target, score, status)
               VALUES (?,?,?,?,?,?,?,?,'pending')""",
            (queued_at, symbol, side, quantity, entry, stop, target, score),
        )
        return cur.lastrowid


def list_pending_orders(status: str = "pending", db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM pending_orders WHERE status = ? ORDER BY id ASC", (status,)
        ).fetchall()
        return [dict(r) for r in rows]


def mark_pending_order(order_id: int, status: str, reviewed_at: str, db_path: str = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE pending_orders SET status = ?, reviewed_at = ? WHERE id = ?",
            (status, reviewed_at, order_id),
        )


def recent_candidates(limit: int = 50, db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM candidates ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def recent_trades(limit: int = 50, db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def summary_stats(db_path: str = DEFAULT_DB_PATH) -> dict:
    with _connect(db_path) as conn:
        n, wins, pnl = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END), SUM(realized_pnl) "
            "FROM trades WHERE exit_price IS NOT NULL"
        ).fetchone()
        return {
            "closed_trades": n or 0,
            "wins": wins or 0,
            "win_rate": (wins / n) if n else None,
            "total_realized_pnl": pnl or 0.0,
        }
