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
