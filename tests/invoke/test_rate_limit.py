"""Unit tests for the sliding-window invoke rate limiter."""

from __future__ import annotations

import uuid

import pytest

from cam.invoke.rate_limit import RateLimiter


def test_allows_up_to_limit_then_denies() -> None:
    now = [0.0]
    limiter = RateLimiter(3, clock=lambda: now[0])
    token = uuid.uuid4()
    assert limiter.allow(token) is True
    assert limiter.allow(token) is True
    assert limiter.allow(token) is True
    assert limiter.allow(token) is False
    assert limiter.limit == 3


def test_window_slides_after_expiry() -> None:
    now = [0.0]
    limiter = RateLimiter(2, clock=lambda: now[0])
    token = uuid.uuid4()
    assert limiter.allow(token) is True
    assert limiter.allow(token) is True
    assert limiter.allow(token) is False
    now[0] = 60.5
    assert limiter.allow(token) is True


def test_tokens_have_independent_buckets() -> None:
    now = [0.0]
    limiter = RateLimiter(1, clock=lambda: now[0])
    assert limiter.allow(uuid.uuid4()) is True
    assert limiter.allow(uuid.uuid4()) is True


def test_rejects_invalid_limit() -> None:
    with pytest.raises(ValueError, match="limit_per_min"):
        RateLimiter(0)
