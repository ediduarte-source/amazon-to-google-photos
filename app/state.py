"""Persistência em SQLite para checkpoint, deduplicação por SHA-256 e retomada."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MigrationItem:
    id: int
    file_hash: str
    file_path: str
    file_name: str
    file_size: int
    mime_type: str
    album_name: str
    status: str  # pending | uploading | uploaded | failed | skipped
    google_media_id: str | None = None
    error_message: str | None = None
    updated_at: str = ""


class MigrationStateStore:
    """Armazena o progresso da migração em SQLite para permitir pausar e retomar sem duplicar fotos."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS albums (
                    album_name TEXT PRIMARY KEY,
                    google_album_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS media_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_hash TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    mime_type TEXT NOT NULL,
                    album_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    google_media_id TEXT,
                    error_message TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(file_hash, album_name)
                );

                CREATE INDEX IF NOT EXISTS idx_media_status_album
                    ON media_items(album_name, status);
                """
            )

    def get_album_id(self, album_name: str) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT google_album_id FROM albums WHERE album_name = ?",
                (album_name,),
            ).fetchone()
            return row["google_album_id"] if row else None

    def save_album_id(self, album_name: str, google_album_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO albums (album_name, google_album_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(album_name) DO UPDATE SET
                    google_album_id = excluded.google_album_id
                """,
                (album_name, google_album_id, _utc_now()),
            )

    def upsert_discovered_items(
        self,
        items: Iterable[dict],
        album_name: str,
    ) -> dict[str, int]:
        """Registra arquivos descobertos, ignorando duplicatas já conhecidas pelo SHA-256."""
        added = 0
        duplicates = 0
        now = _utc_now()
        with self._lock, self._connect() as conn:
            for item in items:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO media_items (
                        file_hash, file_path, file_name, file_size, mime_type,
                        album_name, status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        item["file_hash"],
                        str(item["file_path"]),
                        item["file_name"],
                        int(item["file_size"]),
                        item["mime_type"],
                        album_name,
                        now,
                    ),
                )
                if cur.rowcount > 0:
                    added += 1
                else:
                    duplicates += 1
        return {"added": added, "duplicates": duplicates}

    def get_items_by_status(
        self,
        album_name: str,
        statuses: tuple[str, ...] = ("pending",),
    ) -> list[MigrationItem]:
        placeholders = ",".join("?" for _ in statuses)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM media_items
                WHERE album_name = ? AND status IN ({placeholders})
                ORDER BY id ASC
                """,
                (album_name, *statuses),
            ).fetchall()
            return [MigrationItem(**dict(row)) for row in rows]

    def mark_status(
        self,
        item_id: int,
        status: str,
        *,
        google_media_id: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE media_items
                SET status = ?,
                    google_media_id = COALESCE(?, google_media_id),
                    error_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, google_media_id, error_message, _utc_now(), item_id),
            )

    def reset_failed_to_pending(self, album_name: str) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE media_items
                SET status = 'pending', error_message = NULL, updated_at = ?
                WHERE album_name = ? AND status IN ('failed', 'uploading')
                """,
                (_utc_now(), album_name),
            )
            return cur.rowcount

    def clear_album_state(self, album_name: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM media_items WHERE album_name = ?", (album_name,))

    def get_summary(self, album_name: str) -> dict[str, int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS cnt, COALESCE(SUM(file_size), 0) AS total_bytes
                FROM media_items
                WHERE album_name = ?
                GROUP BY status
                """,
                (album_name,),
            ).fetchall()

        summary = {
            "total": 0,
            "pending": 0,
            "uploading": 0,
            "uploaded": 0,
            "failed": 0,
            "skipped": 0,
            "total_bytes": 0,
            "uploaded_bytes": 0,
        }
        for row in rows:
            status = row["status"]
            cnt = int(row["cnt"])
            size = int(row["total_bytes"])
            summary["total"] += cnt
            summary["total_bytes"] += size
            if status in summary:
                summary[status] = cnt
            if status == "uploaded":
                summary["uploaded_bytes"] = size
        return summary

    def get_recent_items(self, album_name: str, limit: int = 50) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, file_name, file_size, status, error_message, updated_at
                FROM media_items
                WHERE album_name = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (album_name, limit),
            ).fetchall()
            return [dict(row) for row in rows]
