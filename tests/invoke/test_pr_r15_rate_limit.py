"""R15: the rate limiter is documented as per-process and pluggable."""

from __future__ import annotations

import uuid

from cam.invoke.rate_limit import RateLimiter, RateLimiterBackend, default_rate_limiter


def test_default_backend_conforms_to_protocol() -> None:
    backend = default_rate_limiter(3)
    assert isinstance(backend, RateLimiterBackend)
    assert isinstance(backend, RateLimiter)


def test_backend_allow_and_limit() -> None:
    backend = default_rate_limiter(2)
    assert backend.limit == 2
    token = uuid.uuid4()
    assert backend.allow(token) is True
    assert backend.allow(token) is True
    assert backend.allow(token) is False
