from __future__ import annotations

import os
import sqlite3
from typing import Optional


_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    listing_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    normalized_address TEXT,
    city TEXT,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_seen_address ON seen(normalized_address, city);
"""


class SeenDB:
    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

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


def reset_seen_db(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)
