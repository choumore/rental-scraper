from __future__ import annotations

import os
import sqlite3
from typing import Optional


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS seen (
    listing_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    normalized_address TEXT,
    city TEXT,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    board_id TEXT,
    status TEXT NOT NULL DEFAULT 'active'
);
"""

_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_seen_address ON seen(normalized_address, city);
CREATE INDEX IF NOT EXISTS idx_seen_status ON seen(status);
CREATE INDEX IF NOT EXISTS idx_seen_last_seen ON seen(last_seen_at);
"""


class SeenDB:
    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path)
        # Order matters: create-table-if-needed, then migrate (adds columns
        # to legacy DBs), then create indexes (which reference those columns).
        self._conn.executescript(_CREATE_TABLE)
        self._migrate()
        self._conn.executescript(_CREATE_INDEXES)
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns to legacy seen.db files. Idempotent."""
        cur = self._conn.execute("PRAGMA table_info(seen)")
        cols = {row[1] for row in cur.fetchall()}
        if "last_seen_at" not in cols:
            self._conn.execute("ALTER TABLE seen ADD COLUMN last_seen_at TIMESTAMP")
            self._conn.execute(
                "UPDATE seen SET last_seen_at = first_seen_at WHERE last_seen_at IS NULL"
            )
        if "board_id" not in cols:
            self._conn.execute("ALTER TABLE seen ADD COLUMN board_id TEXT")
        if "status" not in cols:
            self._conn.execute("ALTER TABLE seen ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")

    def close(self) -> None:
        self._conn.close()

    def is_seen_by_id(self, listing_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM seen WHERE listing_id = ? LIMIT 1",
            (listing_id,),
        )
        return cur.fetchone() is not None

    def is_seen_by_address(
        self, normalized_address: str, city: str
    ) -> Optional[str]:
        """Return the listing_id that first claimed this (address, city), or None.

        Empty normalized_address always returns None — we can't dedup without an address.
        """
        if not normalized_address:
            return None
        cur = self._conn.execute(
            "SELECT listing_id FROM seen WHERE normalized_address = ? AND city = ? LIMIT 1",
            (normalized_address, city),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def mark_seen(
        self,
        listing_id: str,
        source: str,
        normalized_address: str,
        city: str,
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO seen (listing_id, source, normalized_address, city) VALUES (?, ?, ?, ?)",
            (listing_id, source, normalized_address, city),
        )
        self._conn.commit()

    def count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM seen")
        return cur.fetchone()[0]

    def update_last_seen(self, listing_id: str) -> None:
        """Bump last_seen_at to now. Used every cron run for listings still on the source."""
        self._conn.execute(
            "UPDATE seen SET last_seen_at = CURRENT_TIMESTAMP WHERE listing_id = ?",
            (listing_id,),
        )
        self._conn.commit()

    def set_board_id(self, listing_id: str, board_id: str) -> None:
        self._conn.execute(
            "UPDATE seen SET board_id = ? WHERE listing_id = ?",
            (board_id, listing_id),
        )
        self._conn.commit()

    def get_board_id(self, listing_id: str) -> Optional[str]:
        cur = self._conn.execute(
            "SELECT board_id FROM seen WHERE listing_id = ?", (listing_id,)
        )
        row = cur.fetchone()
        return row[0] if row and row[0] else None

    def mark_status(self, listing_id: str, status: str) -> None:
        self._conn.execute(
            "UPDATE seen SET status = ? WHERE listing_id = ?",
            (status, listing_id),
        )
        self._conn.commit()

    def list_all_without_board(self) -> list[tuple[str, str, str, str]]:
        """Return (listing_id, source, normalized_address, city) for rows missing a board.
        Used by backfill."""
        cur = self._conn.execute(
            "SELECT listing_id, source, normalized_address, city FROM seen "
            "WHERE board_id IS NULL OR board_id = '' "
            "ORDER BY first_seen_at DESC"
        )
        return cur.fetchall()

    def list_stale_active(self, days: int) -> list[tuple[str, str]]:
        """Return (listing_id, board_id) where status='active', has a board, and
        last_seen_at is older than N days. Used to flag boards as Gone."""
        cur = self._conn.execute(
            "SELECT listing_id, board_id FROM seen "
            "WHERE status = 'active' AND board_id IS NOT NULL AND board_id != '' "
            "AND last_seen_at < datetime('now', ?) ",
            (f"-{int(days)} days",),
        )
        return cur.fetchall()


def reset_seen_db(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)
