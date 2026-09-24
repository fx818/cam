"""Sliding-window rate limit for invoke calls, keyed by CAM token id.

Only `invoke` actions are counted; list and describe stay cheap. The window
is sixty seconds of monotonic timestamps, pruned on each check. Entries for
idle tokens are bounded by the number of issued tokens.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        limit_per_min: int,
        *,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit_per_min < 1:
            raise ValueError("limit_per_min must be >= 1")
        self._limit = limit_per_min
        self._window_s = window_s
        self._clock = clock
        self._hits: dict[uuid.UUID, deque[float]] = {}

    @property
    def limit(self) -> int:
        return self._limit

    def allow(self, token_id: uuid.UUID) -> bool:
        now = self._clock()
        window = self._hits.get(token_id)
        if window is None:
            window = deque[float]()
            self._hits[token_id] = window
        cutoff = now - self._window_s
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= self._limit:
            return False
        window.append(now)
        return True
