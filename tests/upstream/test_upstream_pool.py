"""Session pool behaviour: reuse, reconnect-once, idle reaping, concurrency."""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator

import pytest

from cam.upstream import ConnectionTarget, SessionPool, UpstreamSession, UpstreamUnavailableError
from cam.upstream.errors import UpstreamError
from fixtures.mock_mcp import MockMCPHandle


def make_target(handle: MockMCPHandle, **overrides: object) -> ConnectionTarget:
    base: dict[str, object] = {
        "service_id": "test/mock",
        "connection_id": f"conn-{uuid.uuid4().hex[:8]}",
        "url": handle.streamable_url,
        "transport": "streamable-http",
    }
    base.update(overrides)
    return ConnectionTarget(**base)  # type: ignore[arg-type]


@pytest.fixture
async def pool() -> AsyncGenerator[SessionPool]:
    p = SessionPool(idle_timeout_s=60.0, call_timeout_s=10.0)
    yield p
    await p.aclose()


async def test_reuses_session_for_same_key(pool: SessionPool, fresh_counts: MockMCPHandle) -> None:
    target = make_target(fresh_counts)
    first = await pool.session(target)
    second = await pool.session(target)
    assert first is second
    await first.call_tool("echo", {"text": "a"})
    await first.call_tool("echo", {"text": "b"})
    assert fresh_counts.call_counts["echo"] == 2


async def test_separate_keys_get_separate_sessions(
    pool: SessionPool, mock_mcp: MockMCPHandle
) -> None:
    a = await pool.session(make_target(mock_mcp, connection_id="a"))
    b = await pool.session(make_target(mock_mcp, connection_id="b"))
    assert a is not b


async def test_invalidate_closes_and_next_acquire_reconnects(
    pool: SessionPool, mock_mcp: MockMCPHandle
) -> None:
    target = make_target(mock_mcp)
    first = await pool.session(target)
    await pool.invalidate(target)
    assert first.closed
    second = await pool.session(target)
    assert second is not first
    assert not second.closed


async def test_concurrent_requests_share_one_session(
    pool: SessionPool, fresh_counts: MockMCPHandle
) -> None:
    target = make_target(fresh_counts)
    sessions = await asyncio.gather(*(pool.session(target) for _ in range(8)))
    assert all(s is sessions[0] for s in sessions)
    results = await asyncio.gather(
        *(sessions[0].call_tool("slow", {"sleep_s": 0.4}) for _ in range(3))
    )
    assert len(results) == 3
    assert fresh_counts.call_counts["slow"] == 3


async def test_establish_retries_once_then_succeeds(
    pool: SessionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = ConnectionTarget(
        service_id="test/retry",
        connection_id="c1",
        url="http://127.0.0.1:9/mcp",
    )
    calls = {"n": 0}

    async def flaky_open(t: ConnectionTarget) -> UpstreamSession:
        calls["n"] += 1
        if calls["n"] == 1:
            raise UpstreamUnavailableError("first attempt failed")
        # Second attempt succeeds without a network connect.
        return UpstreamSession(t, call_timeout_s=pool.call_timeout_s)

    monkeypatch.setattr(pool, "_open_session", flaky_open)
    session = await pool.session(target)
    assert calls["n"] == 2
    assert session is not None


async def test_establish_fails_after_single_retry(
    pool: SessionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = ConnectionTarget(
        service_id="test/retry-fail",
        connection_id="c1",
        url="http://127.0.0.1:9/mcp",
    )
    calls = {"n": 0}

    async def always_fail(t: ConnectionTarget) -> UpstreamSession:
        calls["n"] += 1
        raise UpstreamUnavailableError(f"attempt {calls['n']}")

    monkeypatch.setattr(pool, "_open_session", always_fail)
    with pytest.raises(UpstreamUnavailableError, match="attempt 2"):
        await pool.session(target)
    assert calls["n"] == 2


async def test_call_failure_is_never_resent(pool: SessionPool, fresh_counts: MockMCPHandle) -> None:
    target = make_target(fresh_counts)
    session = await pool.session(target)
    with pytest.raises(UpstreamError):
        await session.call_tool("slow", {"sleep_s": 10.0}, timeout_s=0.2)
    assert fresh_counts.call_counts["slow"] == 1
    outcome = await session.call_tool("echo", {"text": "still-alive"})
    assert outcome.is_error is False


async def test_reap_idle_closes_stale_sessions(
    pool: SessionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool.idle_timeout_s = 60.0  # pyright: ignore[reportAttributeAccessIssue]
    target = ConnectionTarget(
        service_id="test/idle",
        connection_id="c1",
        url="http://127.0.0.1:9/mcp",
    )

    async def fake_open(target: ConnectionTarget) -> UpstreamSession:
        return UnstartedSession(target, call_timeout_s=pool.call_timeout_s)

    monkeypatch.setattr(pool, "_open_session", fake_open)
    session = await pool.session(target)
    assert not session.closed
    reaper = pool._reaper  # pyright: ignore[reportPrivateUsage]
    if reaper is not None:
        reaper.cancel()
        pool._reaper = None  # pyright: ignore[reportPrivateUsage]
        with contextlib.suppress(asyncio.CancelledError):
            await reaper
    pool.idle_timeout_s = 0.05  # pyright: ignore[reportAttributeAccessIssue]
    await asyncio.sleep(0.06)
    closed = await pool.reap_idle()
    assert closed == 1
    assert session.closed


class UnstartedSession(UpstreamSession):
    """Session that skips network connect; used for idle-reap unit tests."""

    async def start(self, *, timeout_s: float | None = None) -> None:
        del timeout_s
        self._client = object()  # type: ignore[assignment]
        self._closed = False
        self.touch()


async def test_aclose_closes_all(pool: SessionPool, mock_mcp: MockMCPHandle) -> None:
    a = await pool.session(make_target(mock_mcp, connection_id="a"))
    b = await pool.session(make_target(mock_mcp, connection_id="b"))
    await pool.aclose()
    assert a.closed
    assert b.closed


async def test_settings_defaults_used(mock_mcp: MockMCPHandle) -> None:
    from cam.config import Settings

    settings = Settings()
    p = SessionPool(settings=settings)
    try:
        assert p.idle_timeout_s == float(settings.session_idle_timeout_s)
        assert p.call_timeout_s == float(settings.default_timeout_s)
        target = make_target(mock_mcp, connection_id="settings")
        session = await p.session(target)
        assert session.healthy
    finally:
        await p.aclose()
