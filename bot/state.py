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

import calendar
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
    entry_fee: float = 0.0    # fee paid to OPEN (quote ccy) — used for net P/L at close
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
                high_water REAL DEFAULT 0, entry_fee REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, side TEXT, size REAL,
                entry_price REAL, exit_price REAL, pnl REAL, reason TEXT,
                opened_at REAL, closed_at REAL,
                gross_pnl REAL DEFAULT 0, fees REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS daily (
                day TEXT PRIMARY KEY, start_equity REAL,
                realized_pnl REAL DEFAULT 0, consecutive_losses INTEGER DEFAULT 0
            );
            """
        )
        # Migrations for DBs created before a column existed (harmless if present).
        # trades.pnl now stores NET P/L (after fees); gross_pnl/fees are the split.
        for stmt in (
            "ALTER TABLE positions ADD COLUMN high_water REAL DEFAULT 0",
            "ALTER TABLE positions ADD COLUMN entry_fee REAL DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN gross_pnl REAL DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN fees REAL DEFAULT 0",
        ):
            try:
                c.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _today_start_epoch() -> float:
    """Unix timestamp of 00:00:00 UTC today — the cutoff for 'today's' rows."""
    return float(calendar.timegm(time.strptime(_today(), "%Y-%m-%d")))


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
            "opened_at, client_oid, high_water, entry_fee) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (p.symbol, p.side, p.size, p.entry_price, p.stop, p.target, p.opened_at,
             p.client_oid, p.high_water or p.entry_price, p.entry_fee),
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
            high_water=r["high_water"], entry_fee=r["entry_fee"],
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


def remove_position(position_id: int, path: str | None = None) -> None:
    """Delete an open position WITHOUT recording a trade or any P/L.

    For reconciliation only — e.g. a position recovered on startup that the
    exchange does not actually hold (a phantom: the buy never really filled, or
    the coin was moved/sold outside the bot). It was never a real trade, so it
    must not book a fake P/L into the daily total or the stats.
    """
    with _connect(path) as c:
        c.execute("DELETE FROM positions WHERE id=?", (position_id,))


def count_open(path: str | None = None) -> int:
    with _connect(path) as c:
        return int(c.execute("SELECT COUNT(*) FROM positions").fetchone()[0])


def has_open_symbol(symbol: str, path: str | None = None) -> bool:
    with _connect(path) as c:
        row = c.execute("SELECT 1 FROM positions WHERE symbol=? LIMIT 1", (symbol,)).fetchone()
    return row is not None


def close_position(
    position_id: int,
    exit_price: float,
    reason: str,
    path: str | None = None,
    *,
    entry_fee: float = 0.0,
    exit_fee: float = 0.0,
) -> float:
    """Move an open position to `trades`, update daily P/L + streak.

    Records NET realized P/L = gross − (entry_fee + exit_fee), where gross is
    the long-only price move (exit − entry) × size. The NET figure is what
    drives the daily-loss limit, the consecutive-loss streak, the stats, and
    the trade history; the gross figure and the total fee are stored alongside
    it for transparency. Returns the NET pnl.
    """
    with _connect(path) as c:
        r = c.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        if r is None:
            return 0.0
        gross = (exit_price - r["entry_price"]) * r["size"]  # long-only, pre-fee
        fees = (entry_fee or 0.0) + (exit_fee or 0.0)
        net = gross - fees
        now = time.time()
        c.execute(
            "INSERT INTO trades (symbol, side, size, entry_price, exit_price, pnl, "
            "reason, opened_at, closed_at, gross_pnl, fees) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (r["symbol"], r["side"], r["size"], r["entry_price"], exit_price, net, reason,
             r["opened_at"], now, gross, fees),
        )
        c.execute("DELETE FROM positions WHERE id=?", (position_id,))
        # Daily P/L and the losing-streak use NET, so fees count against limits.
        streak_delta = "consecutive_losses + 1" if net < 0 else "0"
        c.execute(
            f"UPDATE daily SET realized_pnl = realized_pnl + ?, "
            f"consecutive_losses = {streak_delta} WHERE day=?",
            (net, _today()),
        )
    return net


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


def orders_today(path: str | None = None) -> int:
    """Number of entry (BUY) orders opened since 00:00 UTC today.

    Counts both positions still open and positions already closed today, so
    the daily cap survives a restart and can't be reset by closing trades.
    Each entry is one money-spending order — that's what MAX_DAILY_ORDERS caps.
    """
    start = _today_start_epoch()
    with _connect(path) as c:
        still_open = c.execute(
            "SELECT COUNT(*) FROM positions WHERE opened_at >= ?", (start,)
        ).fetchone()[0]
        closed = c.execute(
            "SELECT COUNT(*) FROM trades WHERE opened_at >= ?", (start,)
        ).fetchone()[0]
    return int(still_open) + int(closed)


# ---- dashboard helpers ---------------------------------------------------
def recent_trades(limit: int = 20, path: str | None = None) -> list[dict]:
    with _connect(path) as c:
        rows = c.execute(
            "SELECT * FROM trades ORDER BY closed_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def stats(path: str | None = None) -> dict:
    with _connect(path) as c:
        rows = c.execute("SELECT pnl, gross_pnl, fees FROM trades").fetchall()
    pnls = [r["pnl"] for r in rows]                 # NET (after fees)
    wins = [p for p in pnls if p > 0]               # win/loss judged on NET
    return {
        "total_trades": len(pnls),
        "win_rate": (len(wins) / len(pnls) * 100) if pnls else 0.0,
        "total_pnl": sum(pnls),                     # NET total
        "gross_pnl": sum((r["gross_pnl"] or 0.0) for r in rows),
        "total_fees": sum((r["fees"] or 0.0) for r in rows),
        "open_positions": count_open(path),
    }
