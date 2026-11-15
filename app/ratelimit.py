"""Per-caller rate limit: a sliding window of requests, kept in this process.

Keyed by the authenticated `sub` (never by IP, never by anything the caller writes), so one customer cannot use up
the model quota of the others. It runs after authentication: an unauthenticated request never gets here.

In memory on purpose: the API runs ONE worker (the SQLite checkpointer needs that too), so a shared store would add a
service and a dependency without changing what is protected. With several workers each one would count on its own
(the effective limit becomes N times higher); that is the point where this class is replaced by a Redis-backed one
with the same `hit` method.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

MAX_KEYS = 10_000  # more distinct callers than this: forget the idle ones first


class RateLimiter:
    def __init__(self, per_minute: int, window: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.limit, self.window, self._clock = per_minute, window, clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> float:
        """Count one request. Returns 0.0 if it is allowed, else the seconds to wait. `limit <= 0` means no limit."""
        if self.limit <= 0:
            return 0.0
        with self._lock:
            now = self._clock()
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return max(0.001, self.window - (now - q[0]))  # a refused request is not counted
            q.append(now)
            if len(self._hits) > MAX_KEYS:
                self._forget_idle(now)
            return 0.0

    def _forget_idle(self, now: float) -> None:
        for k in [k for k, d in self._hits.items() if not d or now - d[-1] >= self.window]:
            del self._hits[k]
