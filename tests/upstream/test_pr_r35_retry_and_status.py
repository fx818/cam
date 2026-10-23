"""R35 regression: one retry owner, cancellation-safe establish, and
per-request HTTP-status correlation (no session-wide drain)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack

import httpx2
import pytest
from mcp.client import Transport

from cam.upstream import ConnectionTarget, SessionPool, UpstreamSession
from cam.upstream import client as client_mod
from cam.upstream.errors import UpstreamAuthError, UpstreamTimeoutError
from fixtures.mock_mcp import MockMCPHandle

_capture_statuses = client_mod._capture_statuses  # pyright: ignore[reportPrivateUsage]
_status_recorder = client_mod._status_recorder  # pyright: ignore[reportPrivateUsage]
_status_ctx = client_mod._status_ctx  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
async def pool() -> AsyncGenerator[SessionPool]:
    p = SessionPool(idle_timeout_s=60.0, call_timeout_s=10.0)
    yield p
    await p.aclose()


def make_target(handle: MockMCPHandle, **overrides: object) -> ConnectionTarget:
    base: dict[str, object] = {
        "service_id": "test/mock",
        "connection_id": "c1",
        "url": handle.streamable_url,
        "transport": "streamable-http",
    }
    base.update(overrides)
    return ConnectionTarget(**base)  # type: ignore[arg-type]


async def test_status_recorder_routes_to_current_call_sink() -> None:
    fallback: list[int] = []
    record = _status_recorder(fallback)

    sink_a: list[int] = []
    token = _status_ctx.set(sink_a)
    await record(httpx2.Response(401))
    _status_ctx.reset(token)

    sink_b: list[int] = []
    token = _status_ctx.set(sink_b)
    await record(httpx2.Response(200))
    _status_ctx.reset(token)

    await record(httpx2.Response(500))  # no active call -> session fallback
    assert sink_a == [401]
    assert sink_b == [200]
    assert fallback == [500]


async def test_status_capture_is_isolated_between_concurrent_tasks() -> None:
    fallback: list[int] = []
    record = _status_recorder(fallback)
    results: dict[str, list[int]] = {}

    async def worker(name: str, status: int, delay: float) -> None:
        with _capture_statuses(fallback) as sink:
            await asyncio.sleep(delay)
            await record(httpx2.Response(status))
            results[name] = list(sink)

    await asyncio.gather(worker("a", 401, 0.05), worker("b", 200, 0.0))
    assert results == {"a": [401], "b": [200]}
    assert fallback == []


def test_raise_mapped_uses_per_call_statuses_not_shared_list() -> None:
    target = ConnectionTarget(service_id="s", connection_id="c", url="http://127.0.0.1:9/mcp")
    session = UpstreamSession(target, call_timeout_s=1.0)
    session._statuses.extend([200, 200])  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(UpstreamAuthError):
        session._raise_mapped(RuntimeError("boom"), [401])  # pyright: ignore[reportPrivateUsage]
    # The session-wide list is untouched: a concurrent call's statuses are
    # never drained or misattributed.
    assert session._statuses == [200, 200]  # pyright: ignore[reportPrivateUsage]


async def test_pool_skips_retry_when_establish_already_reconnected(
    pool: SessionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    async def open_once(target: ConnectionTarget) -> UpstreamSession:
        calls["n"] += 1
        raise UpstreamTimeoutError("legacy + fallback both failed", retries_exhausted=True)

    monkeypatch.setattr(pool, "_open_session", open_once)
    target = ConnectionTarget(
        service_id="test/one-retry", connection_id="c1", url="http://127.0.0.1:9/mcp"
    )
    with pytest.raises(UpstreamTimeoutError):
        await pool.session(target)
    assert calls["n"] == 1


async def test_pool_bounds_compounded_establish_retries(
    pool: SessionPool, mock_mcp: MockMCPHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single connect must not make four attempts (legacy + fallback, twice)."""
    attempts: list[int] = []

    def fake_build(self: UpstreamSession, stack: AsyncExitStack, statuses: list[int]) -> Transport:
        attempts.append(1)
        raise TimeoutError()

    monkeypatch.setattr(  # pyright: ignore[reportPrivateUsage]
        UpstreamSession, "_build_transport", fake_build
    )
    target = make_target(mock_mcp, service_id="test/bound", url=mock_mcp.streamable_url)
    with pytest.raises(UpstreamTimeoutError):
        await pool.session(target)
    # legacy attempt + its one mode fallback = 2; the pool adds no third.
    assert len(attempts) == 2


async def test_cancelling_start_closes_the_owner_task(
    mock_mcp_hang_get: MockMCPHandle,
) -> None:
    target = ConnectionTarget(
        service_id="test/cancel",
        connection_id="c1",
        url=mock_mcp_hang_get.streamable_url,
    )
    session = UpstreamSession(target, call_timeout_s=10.0)
    task = asyncio.create_task(session.start(timeout_s=5.0))
    await asyncio.sleep(0.3)  # inside the hung legacy establish
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.closed
    assert session._owner is None  # pyright: ignore[reportPrivateUsage]
