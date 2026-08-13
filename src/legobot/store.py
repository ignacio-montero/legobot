"""SQLite persistence: which sets are tracked, what we last saw, and bot state.

Why SQLite and not a JSON file: we write from one process but we write *often*
(every scan updates every tracked row), and a torn write would lose the tracking
list. SQLite gives atomic transactions for free. Why not Postgres: this is a
single-user bot with tens of rows on a 8 GB box — a server process would be pure
overhead. Same call as Blue Plaque Hunter.

The DB is deliberately tiny and boring; all of it fits in memory many times over.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked (
    product_id      TEXT PRIMARY KEY,
    url_part        TEXT NOT NULL,
    name            TEXT NOT NULL,
    added_at        INTEGER NOT NULL,
    last_available  INTEGER,          -- 0/1/NULL(never scanned)
    last_seen_at    INTEGER,          -- last scan that found this product
    notified_at     INTEGER           -- last time we sent an "it's available" alert
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class TrackedSet:
    product_id: str
    url_part: str
    name: str
    added_at: int
    last_available: Optional[bool]
    last_seen_at: Optional[int]
    notified_at: Optional[int]

    @property
    def url(self) -> str:
        return f"https://www.brickborrow.com/product-page/{self.url_part}"


def _row_to_tracked(row: sqlite3.Row) -> TrackedSet:
    la = row["last_available"]
    return TrackedSet(
        product_id=row["product_id"],
        url_part=row["url_part"],
        name=row["name"],
        added_at=row["added_at"],
        last_available=None if la is None else bool(la),
        last_seen_at=row["last_seen_at"],
        notified_at=row["notified_at"],
    )


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL keeps readers unblocked and survives an ungraceful container stop
        # far better than the default rollback journal.
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---------------- tracked sets ----------------

    def list_tracked(self) -> list[TrackedSet]:
        rows = self._conn.execute(
            "SELECT * FROM tracked ORDER BY added_at ASC"
        ).fetchall()
        return [_row_to_tracked(r) for r in rows]

    def count_tracked(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM tracked").fetchone()[0])

    def get(self, product_id: str) -> Optional[TrackedSet]:
        row = self._conn.execute(
            "SELECT * FROM tracked WHERE product_id = ?", (product_id,)
        ).fetchone()
        return _row_to_tracked(row) if row else None

    def add(self, product_id: str, url_part: str, name: str) -> bool:
        """Insert a tracked set. Returns False if it was already tracked."""
        try:
            self._conn.execute(
                "INSERT INTO tracked (product_id, url_part, name, added_at) "
                "VALUES (?, ?, ?, ?)",
                (product_id, url_part, name, int(time.time())),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def remove(self, product_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM tracked WHERE product_id = ?", (product_id,))
        return cur.rowcount > 0

    def clear_tracked(self) -> int:
        cur = self._conn.execute("DELETE FROM tracked")
        return cur.rowcount

    def record_scan(
        self,
        product_id: str,
        *,
        available: bool,
        name: Optional[str] = None,
        url_part: Optional[str] = None,
        notified: bool = False,
    ) -> None:
        now = int(time.time())
        sets = ["last_available = ?", "last_seen_at = ?"]
        args: list[object] = [1 if available else 0, now]
        if name is not None:
            sets.append("name = ?")
            args.append(name)
        if url_part is not None:
            sets.append("url_part = ?")
            args.append(url_part)
        if notified:
            sets.append("notified_at = ?")
            args.append(now)
        args.append(product_id)
        self._conn.execute(
            f"UPDATE tracked SET {', '.join(sets)} WHERE product_id = ?", args
        )

    # ---------------- meta / bot state ----------------

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @property
    def paused(self) -> bool:
        return self.get_meta("paused", "0") == "1"

    def set_paused(self, value: bool) -> None:
        self.set_meta("paused", "1" if value else "0")

    @property
    def telegram_offset(self) -> int:
        """Telegram getUpdates cursor, persisted so a restart doesn't replay commands."""
        return int(self.get_meta("telegram_offset", "0") or 0)

    def set_telegram_offset(self, value: int) -> None:
        self.set_meta("telegram_offset", str(value))
