"""SQLite-backed persistent download queue.

The queue survives restarts and crashes, which is what makes a 1500-song
playlist manageable: you can pause, close the browser, reboot, and pick up
exactly where you left off.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Iterable

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    source      TEXT NOT NULL,          -- youtube | spotify | text | folder
    source_url  TEXT,
    added_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL,
    url         TEXT NOT NULL,
    source      TEXT NOT NULL,
    title       TEXT,
    artist      TEXT,
    album       TEXT,
    duration    REAL,
    thumbnail   TEXT,
    track_number INTEGER,
    mode        TEXT NOT NULL DEFAULT 'audio',     -- audio | video
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending|downloading|done|failed|skipped
    progress    REAL NOT NULL DEFAULT 0,
    speed       REAL,
    eta         REAL,
    filepath    TEXT,
    error       TEXT,
    retries     INTEGER NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    added_at    REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_playlist ON items(playlist_id);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
"""


class QueueDB:
    def __init__(self, path=None) -> None:
        self._path = path or paths.database_file()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    # Columns added after the first release; safe to re-run.
    _MIGRATIONS = {
        "items": {
            "thumbnail": "TEXT",
            "track_number": "INTEGER",
            "mode": "TEXT NOT NULL DEFAULT 'audio'",
        },
    }

    def _migrate(self) -> None:
        for table, columns in self._MIGRATIONS.items():
            existing = {
                row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            for name, decl in columns.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- raw helpers -------------------------------------------------------
    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    # -- playlists ---------------------------------------------------------
    def add_playlist(self, name: str, source: str, source_url: str | None = None) -> int:
        with self._lock:
            cur = self._execute(
                "INSERT INTO playlists (name, source, source_url, added_at) VALUES (?,?,?,?)",
                (name, source, source_url, time.time()),
            )
            return cur.lastrowid

    def list_playlists(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._query(
                """SELECT p.*, COUNT(i.id) AS total,
                          SUM(i.status='done') AS done,
                          SUM(i.status='skipped') AS skipped,
                          SUM(i.status='pending') AS pending,
                          SUM(i.status='failed') AS failed
                   FROM playlists p LEFT JOIN items i ON i.playlist_id = p.id
                   GROUP BY p.id ORDER BY p.id DESC"""
            )
            out = []
            for r in rows:
                d = dict(r)
                for k in ("total", "done", "skipped", "pending", "failed"):
                    d[k] = int(d[k] or 0)
                out.append(d)
            return out

    def get_playlist(self, playlist_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._query("SELECT * FROM playlists WHERE id=?", (playlist_id,))
            return dict(row[0]) if row else None

    def delete_playlist(self, playlist_id: int) -> None:
        with self._lock:
            self._execute("DELETE FROM items WHERE playlist_id=?", (playlist_id,))
            self._execute("DELETE FROM playlists WHERE id=?", (playlist_id,))

    # -- items -------------------------------------------------------------
    def add_items(self, playlist_id: int, items: Iterable[dict[str, Any]]) -> int:
        """Bulk insert, ignoring exact duplicate URLs already in the queue."""
        now = time.time()
        added = 0
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT MAX(sort_order) FROM items WHERE playlist_id=?", (playlist_id,))
            start = (cur.fetchone()[0] or 0) + 1
            existing = {
                row[0]
                for row in cur.execute(
                    "SELECT url FROM items WHERE playlist_id=?", (playlist_id,)
                ).fetchall()
            }
            batch = []
            for i, it in enumerate(items):
                url = str(it.get("url") or "").strip()
                if not url or url in existing:
                    continue
                existing.add(url)
                batch.append(
                    (
                        playlist_id,
                        url,
                        it.get("source", "unknown"),
                        it.get("title"),
                        it.get("artist"),
                        it.get("album"),
                        it.get("duration"),
                        it.get("thumbnail"),
                        it.get("track_number"),
                        it.get("mode", "audio"),
                        it.get("status", "pending"),
                        start + i,
                        now,
                        now,
                    )
                )
            cur.executemany(
                """INSERT INTO items
                   (playlist_id, url, source, title, artist, album, duration,
                    thumbnail, track_number, mode, status, sort_order,
                    added_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                batch,
            )
            self._conn.commit()
            added = cur.rowcount
        return added

    def count_items(self, playlist_id: int) -> int:
        with self._lock:
            row = self._query(
                "SELECT COUNT(*) FROM items WHERE playlist_id=?", (playlist_id,)
            )
            return int(row[0][0])

    def get_items(
        self,
        playlist_id: int | None = None,
        status: str | None = None,
        limit: int = 2000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM items WHERE 1=1"
        params: list = []
        if playlist_id is not None:
            sql += " AND playlist_id=?"
            params.append(playlist_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY sort_order LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._lock:
            return [dict(r) for r in self._query(sql, tuple(params))]

    def all_items(self, playlist_id: int) -> list[dict[str, Any]]:
        return self.get_items(playlist_id, limit=10**6)

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._query("SELECT * FROM items WHERE id=?", (item_id,))
            return dict(row[0]) if row else None

    def update_item(self, item_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._execute(
                f"UPDATE items SET {cols} WHERE id=?",
                (*fields.values(), item_id),
            )

    def mark_skipped(self, item_id: int) -> None:
        self.update_item(item_id, status="skipped", progress=0)

    def mark_done(self, item_id: int, filepath: str | None) -> None:
        self.update_item(item_id, status="done", progress=100, filepath=filepath,
                         speed=None, eta=None, error=None)

    def mark_failed(self, item_id: int, error: str, retries: int = 0) -> None:
        self.update_item(item_id, status="failed", error=error[:2000],
                         speed=None, eta=None, retries=retries)

    def mark_pending(self, item_id: int) -> None:
        self.update_item(item_id, status="pending", progress=0, error=None)

    def retry_failed(self, playlist_id: int | None = None) -> int:
        sql = "UPDATE items SET status='pending', error=NULL WHERE status='failed'"
        params: tuple = ()
        if playlist_id is not None:
            sql += " AND playlist_id=?"
            params = (playlist_id,)
        with self._lock:
            cur = self._execute(sql, params)
            return cur.rowcount

    def reset_playlist(self, playlist_id: int, keep_files: bool = True) -> int:
        """Mark everything pending again. If keep_files is False the downloaded
        file paths are cleared too, so history is fully reset."""
        if keep_files:
            fields = "status='pending', progress=0, error=NULL"
        else:
            fields = "status='pending', progress=0, error=NULL, filepath=NULL"
        with self._lock:
            cur = self._execute(
                f"UPDATE items SET {fields} WHERE playlist_id=?", (playlist_id,)
            )
            return cur.rowcount

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._query(
                """SELECT COUNT(*) n FROM items
                   UNION ALL SELECT COUNT(*) FROM items WHERE status='pending'
                   UNION ALL SELECT COUNT(*) FROM items WHERE status='downloading'
                   UNION ALL SELECT COUNT(*) FROM items WHERE status='done'
                   UNION ALL SELECT COUNT(*) FROM items WHERE status='failed'
                   UNION ALL SELECT COUNT(*) FROM items WHERE status='skipped'"""
            )
            vals = [int(r[0]) for r in rows]
            return {
                "total": vals[0], "pending": vals[1], "downloading": vals[2],
                "done": vals[3], "failed": vals[4], "skipped": vals[5],
            }