"""Denylist of revoked token ids (`jti`).

A token that must stop working before it expires (a stolen laptop, an operator who left) is added here by an operator
(`POST /operator/revocations`). An entry only has to live as long as the token could: the API refuses tokens that last
longer than JWT_MAX_LIFETIME_SECONDS, so that is the time an entry is kept.

In memory: after a restart the list is empty, and a revoked token would work again until it expires (at most
JWT_MAX_LIFETIME_SECONDS, 15 minutes by default). Revoke the session in the identity provider too.
"""
from __future__ import annotations

import threading
import time
from typing import Callable


class Denylist:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._until: dict[str, float] = {}
        self._clock = clock
        self._lock = threading.Lock()

    def revoke(self, jti: str, ttl_seconds: float) -> None:
        with self._lock:
            now = self._clock()
            self._until = {k: t for k, t in self._until.items() if t > now}  # drop what already expired
            self._until[jti] = now + ttl_seconds

    def is_revoked(self, jti: str) -> bool:
        with self._lock:
            until = self._until.get(jti)
            return until is not None and until > self._clock()
