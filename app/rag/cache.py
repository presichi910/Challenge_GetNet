"""A small on-disk cache for what Voyage computes: query embeddings and rerank scores.

Voyage's free tier allows ~3 requests a minute, so the same query must never be paid for twice (the eval asks the same
questions over and over; a customer repeats a question). Only a HASH of the query is stored, never its text, together with
the vector or the scores: nothing a customer wrote can be read back from this file. `path=""` turns the cache off.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path


def digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


class DiskCache:
    def __init__(self, path: str | Path = ""):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                db.execute("CREATE TABLE IF NOT EXISTS kv (kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
                           "PRIMARY KEY (kind, key))")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path), timeout=10)

    def get(self, kind: str, key: str):
        if not self.path:
            return None
        try:
            with self._lock, self._connect() as db:
                row = db.execute("SELECT value FROM kv WHERE kind = ? AND key = ?", (kind, key)).fetchone()
            return json.loads(row[0]) if row else None
        except (sqlite3.Error, ValueError):
            return None  # a damaged or locked cache is a miss, never an error

    def put(self, kind: str, key: str, value) -> None:
        if not self.path:
            return
        try:
            with self._lock, self._connect() as db:
                db.execute("INSERT OR REPLACE INTO kv (kind, key, value) VALUES (?, ?, ?)", (kind, key, json.dumps(value)))
        except sqlite3.Error:
            pass
