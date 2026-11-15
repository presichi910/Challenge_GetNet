"""Denylist of revoked token ids (`jti`), kept in SQLite so it survives a restart.

A token that must stop working before it expires (a stolen laptop, an operator who left) is added here by an operator
(`POST /operator/revocations`). An entry only has to live as long as the token could: the API refuses tokens that last
longer than JWT_MAX_LIFETIME_SECONDS, so that is the time an entry is kept; older entries are pruned (on every
revocation and when the file is opened).

The file holds only the `jti` and the moment its entry expires: no token, no claim, no customer id. It lives in the
`data` volume next to the other SQLite files. With no path (the tests) it is an in-memory database.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable


class Denylist:
    def __init__(self, path: str | Path | None = None, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False, isolation_level=None)
        if path:
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS revoked (jti TEXT PRIMARY KEY, expires_at REAL NOT NULL)")
        self._db.execute("CREATE INDEX IF NOT EXISTS revoked_expires ON revoked (expires_at)")
        self._prune()

    def _prune(self) -> int:
        return self._db.execute("DELETE FROM revoked WHERE expires_at <= ?", (self._clock(),)).rowcount

    def revoke(self, jti: str, ttl_seconds: float) -> None:
        with self._lock:
            self._prune()  # drop what already expired: the token is dead by then anyway
            self._db.execute("INSERT OR REPLACE INTO revoked (jti, expires_at) VALUES (?, ?)", (jti, self._clock() + ttl_seconds))

    def is_revoked(self, jti: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM revoked WHERE jti = ? AND expires_at > ?", (jti, self._clock())).fetchone() is not None

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM revoked").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()
