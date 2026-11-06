"""The API's rate limiter: requests counted by a key in a sliding window, in memory (a restart forgets them).

Lifted from solarlytics-backend at c853130, routes_accept.py lines 80-110 (the `Limiter` class), so that the lifted auth.py needs no route module.
"""
from __future__ import annotations

import threading
import time


class Limiter:
    MAX_KEYS = 20000

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._hits: dict[tuple, list[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: tuple, limit: int, window: int) -> int:
        """Seconds to wait, or 0 when the request may go on (and it is counted)."""
        now = self.clock()
        with self._lock:
            if len(self._hits) > self.MAX_KEYS:
                self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > now - window}
            kept = [t for t in self._hits.get(key, []) if t > now - window]
            if len(kept) >= limit:
                self._hits[key] = kept
                return max(1, int(kept[0] + window - now) + 1)
            kept.append(now)
            self._hits[key] = kept
            return 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
