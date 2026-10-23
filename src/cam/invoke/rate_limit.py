"""Sliding-window rate limit for invoke calls, keyed by CAM token id.

Only `invoke` actions are counted; list and describe stay cheap. The window
is sixty seconds of monotonic timestamps, pruned on each check. Entries for
idle tokens are bounded by the number of issued tokens.

IMPORTANT (R15): this limiter is **in-memory and per-process**. With more than
one CAM replica the effective limit is `replicas * limit_per_min`, and a token's
state is not shared across pods. Deployment therefore pins the HPA to a single
replica until a distributed limiter exists. Do not add Redis here; to scale out,
provide a :class:`RateLimiterBackend` implementation backed by shared state and
pass it to the gateway (see `default_rate_limiter`) — the call site only needs
`allow()` and `limit`.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class RateLimiterBackend(Protocol):
    """The only surface the gateway depends on, so the backend is pluggable."""

    @property
    def limit(self) -> int: ...

    def allow(self, token_id: uuid.UUID) -> bool: ...


class RateLimiter:
    """In-process sliding-window limiter (default backend)."""

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


def default_rate_limiter(limit_per_min: int) -> RateLimiterBackend:
    """Return the default (single-replica, in-process) limiter backend.

    Swap this for a distributed backend before running more than one replica.
    """
    return RateLimiter(limit_per_min)
