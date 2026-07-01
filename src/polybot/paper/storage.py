"""SQLite persistence for paper positions / decision log."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_open TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT,
    question TEXT,
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    model_prob REAL NOT NULL,
    edge REAL NOT NULL,
    size_usd REAL NOT NULL,
    shares REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    strategy TEXT,
    rationale TEXT,
    group_key TEXT,
    ts_close TEXT,
    exit_price REAL,
    pnl_usd REAL,
    outcome TEXT,
    close_reason TEXT,
    mode TEXT NOT NULL DEFAULT 'paper'
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
"""

# Columns written on INSERT (open).
_COLS = [
    "ts_open", "market_id", "token_id", "question", "side", "entry_price",
    "model_prob", "edge", "size_usd", "shares", "status", "strategy",
    "rationale", "group_key", "mode",
]


@dataclass
class Position:
    market_id: str
    question: str
    side: str
    entry_price: float
    model_prob: float
    edge: float
    size_usd: float
    shares: float
    ts_open: str
    token_id: str | None = None
    status: str = "open"
    strategy: str = ""
    rationale: str = ""
    group_key: str | None = None
    ts_close: str | None = None
    exit_price: float | None = None
    pnl_usd: float | None = None
    outcome: str | None = None
    close_reason: str | None = None
    mode: str = "paper"
    id: int | None = None


class Storage:
    def __init__(self, path: str) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created."""
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(positions)")}
        for col, ddl in (("close_reason", "close_reason TEXT"), ("group_key", "group_key TEXT")):
            if col not in existing:
                self.conn.execute(f"ALTER TABLE positions ADD COLUMN {ddl}")

    def insert_position(self, pos: Position) -> int:
        values = [getattr(pos, c) for c in _COLS]
        placeholders = ",".join("?" * len(_COLS))
        cur = self.conn.execute(
            f"INSERT INTO positions ({','.join(_COLS)}) VALUES ({placeholders})",
            values,
        )
        self.conn.commit()
        return int(cur.lastrowid)

    @staticmethod
    def _row_to_pos(row: sqlite3.Row) -> Position:
        return Position(**{k: row[k] for k in row.keys()})

    def open_positions(self) -> list[Position]:
        rows = self.conn.execute("SELECT * FROM positions WHERE status='open'").fetchall()
        return [self._row_to_pos(r) for r in rows]

    def all_positions(self) -> list[Position]:
        rows = self.conn.execute("SELECT * FROM positions ORDER BY id").fetchall()
        return [self._row_to_pos(r) for r in rows]

    def open_market_ids(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT market_id FROM positions WHERE status='open'"
        ).fetchall()
        return {r["market_id"] for r in rows}

    def open_exposure(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(size_usd), 0) AS s FROM positions WHERE status='open'"
        ).fetchone()
        return float(row["s"] or 0.0)

    def exposure_by_group(self) -> dict[str, float]:
        rows = self.conn.execute(
            "SELECT group_key, COALESCE(SUM(size_usd), 0) AS s FROM positions "
            "WHERE status='open' AND group_key IS NOT NULL GROUP BY group_key"
        ).fetchall()
        return {r["group_key"]: float(r["s"] or 0.0) for r in rows}

    def close_position(
        self,
        pos_id: int,
        exit_price: float,
        pnl: float,
        outcome: str,
        ts_close: str,
        close_reason: str,
    ) -> None:
        self.conn.execute(
            "UPDATE positions SET status='closed', exit_price=?, pnl_usd=?, outcome=?, "
            "ts_close=?, close_reason=? WHERE id=?",
            (exit_price, pnl, outcome, ts_close, close_reason, pos_id),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
