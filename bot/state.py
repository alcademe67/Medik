"""Persistent bot state (SQLite) — so the bot recovers cleanly after a
restart and the dashboard has something to read.

Three tables:
  * positions  — currently open positions (survive a restart/crash)
  * trades     — closed trades, for P/L, win rate, and history
  * daily      — per-UTC-day realized P/L and consecutive-loss counter,
                 used by the risk manager's daily-loss and streak limits

Pure storage — it makes no trading decisions. No secrets are ever stored.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass

from bot import config

logger = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    side: str
    size: float
    entry_price: float
    stop: float
    target: float
    opened_at: float
    client_oid: str
    high_water: float = 0.0   # highest price seen since entry (for trailing stops)
    id: int | None = None


def _connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: str | None = None) -> None:
    with _connect(path) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, side TEXT, size REAL, entry_price REAL,
                stop REAL, target REAL, opened_at REAL, client_oid TEXT UNIQUE,
                high_water REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, side TEXT, size REAL,
                entry_price REAL, exit_price REAL, pnl REAL, reason TEXT,
                opened_at REAL, closed_at REAL
            );
            CREATE TABLE IF NOT EXISTS daily (
                day TEXT PRIMARY KEY, start_equity REAL,
                realized_pnl REAL DEFAULT 0, consecutive_losses INTEGER DEFAULT 0
            );
            """
        )
        # Migration for DBs created before high_water existed (harmless if present).
        try:
            c.execute("ALTER TABLE positions ADD COLUMN high_water REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def ensure_day(start_equity: float, path: str | None = None) -> None:
    """Record the day's starting equity once, at first tick of the day."""
    with _connect(path) as c:
        c.execute(
            "INSERT OR IGNORE INTO daily (day, start_equity) VALUES (?, ?)",
            (_today(), start_equity),
        )


# ---- positions -----------------------------------------------------------
def add_position(p: Position, path: str | None = None) -> int:
    with _connect(path) as c:
        cur = c.execute(
            "INSERT INTO positions (symbol, side, size, entry_price, stop, target, "
            "opened_at, client_oid, high_water) VALUES (?,?,?,?,?,?,?,?,?)",
            (p.symbol, p.side, p.size, p.entry_price, p.stop, p.target, p.opened_at,
             p.client_oid, p.high_water or p.entry_price),
        )
        return int(cur.lastrowid)


def open_positions(path: str | None = None) -> list[Position]:
    with _connect(path) as c:
        rows = c.execute("SELECT * FROM positions").fetchall()
    return [
        Position(
            id=r["id"], symbol=r["symbol"], side=r["side"], size=r["size"],
            entry_price=r["entry_price"], stop=r["stop"], target=r["target"],
            opened_at=r["opened_at"], client_oid=r["client_oid"],
            high_water=r["high_water"],
        )
        for r in rows
    ]


def update_stop(position_id: int, new_stop: float, high_water: float, path: str | None = None) -> None:
    """Persist a moved stop and high-water mark (trailing / breakeven)."""
    with _connect(path) as c:
        c.execute(
            "UPDATE positions SET stop=?, high_water=? WHERE id=?",
            (new_stop, high_water, position_id),
        )


def count_open(path: str | None = None) -> int:
    with _connect(path) as c:
        return int(c.execute("SELECT COUNT(*) FROM positions").fetchone()[0])


def has_open_symbol(symbol: str, path: str | None = None) -> bool:
    with _connect(path) as c:
        row = c.execute("SELECT 1 FROM positions WHERE symbol=? LIMIT 1", (symbol,)).fetchone()
    return row is not None


def close_position(position_id: int, exit_price: float, reason: str, path: str | None = None) -> float:
    """Move an open position to `trades`, update daily P/L + streak. Returns pnl."""
    with _connect(path) as c:
        r = c.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        if r is None:
            return 0.0
        pnl = (exit_price - r["entry_price"]) * r["size"]  # long-only
        now = time.time()
        c.execute(
            "INSERT INTO trades (symbol, side, size, entry_price, exit_price, pnl, "
            "reason, opened_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (r["symbol"], r["side"], r["size"], r["entry_price"], exit_price, pnl, reason,
             r["opened_at"], now),
        )
        c.execute("DELETE FROM positions WHERE id=?", (position_id,))
        streak_delta = "consecutive_losses + 1" if pnl < 0 else "0"
        c.execute(
            f"UPDATE daily SET realized_pnl = realized_pnl + ?, "
            f"consecutive_losses = {streak_delta} WHERE day=?",
            (pnl, _today()),
        )
    return pnl


# ---- daily risk state ----------------------------------------------------
def today_realized_pnl(path: str | None = None) -> float:
    with _connect(path) as c:
        r = c.execute("SELECT realized_pnl FROM daily WHERE day=?", (_today(),)).fetchone()
    return float(r["realized_pnl"]) if r else 0.0


def realized_pnl_for_day(day: str, path: str | None = None) -> float:
    """Realized P/L for a specific UTC day (YYYY-MM-DD) — for the daily summary."""
    with _connect(path) as c:
        r = c.execute("SELECT realized_pnl FROM daily WHERE day=?", (day,)).fetchone()
    return float(r["realized_pnl"]) if r else 0.0


def today_start_equity(path: str | None = None) -> float | None:
    with _connect(path) as c:
        r = c.execute("SELECT start_equity FROM daily WHERE day=?", (_today(),)).fetchone()
    return float(r["start_equity"]) if r else None


def consecutive_losses(path: str | None = None) -> int:
    with _connect(path) as c:
        r = c.execute("SELECT consecutive_losses FROM daily WHERE day=?", (_today(),)).fetchone()
    return int(r["consecutive_losses"]) if r else 0


# ---- dashboard helpers ---------------------------------------------------
def recent_trades(limit: int = 20, path: str | None = None) -> list[dict]:
    with _connect(path) as c:
        rows = c.execute(
            "SELECT * FROM trades ORDER BY closed_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def stats(path: str | None = None) -> dict:
    with _connect(path) as c:
        rows = c.execute("SELECT pnl FROM trades").fetchall()
    pnls = [r["pnl"] for r in rows]
    wins = [p for p in pnls if p > 0]
    return {
        "total_trades": len(pnls),
        "win_rate": (len(wins) / len(pnls) * 100) if pnls else 0.0,
        "total_pnl": sum(pnls),
        "open_positions": count_open(path),
    }
