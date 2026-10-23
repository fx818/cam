"""R36 regression: an overall dispatch deadline (not per-phase), a hard cap on
buffered/streamed response bytes, and bounded discovery."""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from _pr_servers import start_recorder, start_streamer

from cam.upstream import client as client_mod
from cam.upstream import rest as rest_mod
from cam.upstream.client import UpstreamSession
from cam.upstream.errors import UpstreamRemoteError, UpstreamTimeoutError
from cam.upstream.rest import execute_rest, fetch_text
from cam.upstream.types import ConnectionTarget, TextBlock
from fixtures.mock_mcp import MockMCPHandle

_read_capped = rest_mod._read_capped  # pyright: ignore[reportPrivateUsage]

BINDING_GET: dict[str, Any] = {"method": "GET", "path": "/anything", "params": {}}


def streamable_target(handle: MockMCPHandle) -> ConnectionTarget:
    return ConnectionTarget(
        service_id="test/caps",
        connection_id="c1",
        url=handle.streamable_url,
        transport="streamable-http",
    )


async def test_read_capped_enforces_limit() -> None:
    response = httpx.Response(200, content=b"0123456789")
    data, truncated = await _read_capped(response, 4)
    assert data == b"0123"
    assert truncated is True
    data, truncated = await _read_capped(httpx.Response(200, content=b"abc"), 16)
    assert data == b"abc"
    assert truncated is False


async def test_overall_deadline_bounds_a_slow_stream() -> None:
    """Per-phase read timeout would not fire (gaps << budget); the overall
    end-to-end deadline must."""
    server = await start_streamer(chunks=20, gap=0.1)  # ~2.0s if unbounded
    try:
        started = time.monotonic()
        with pytest.raises(UpstreamTimeoutError):
            await execute_rest(
                base_url=server.base_url,
                binding=BINDING_GET,
                args={},
                timeout_ms=400,
            )
        elapsed = time.monotonic() - started
        assert 0.3 <= elapsed < 1.5
    finally:
        await server.aclose()


async def test_fetch_text_rejects_body_over_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rest_mod, "MAX_FETCH_BYTES", 256)
    server = await start_recorder(big_body=b"x" * 4096)
    try:
        with pytest.raises(UpstreamRemoteError, match="limit"):
            await fetch_text(f"{server.base_url}/spec")
    finally:
        await server.aclose()


async def test_execute_rest_truncates_body_at_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rest_mod, "MAX_RESPONSE_BYTES", 512)
    server = await start_recorder(big_body=b"y" * 4096)
    try:
        outcome = await execute_rest(
            base_url=server.base_url,
            binding=BINDING_GET,
            args={},
        )
        block = outcome.content[0]
        assert isinstance(block, TextBlock)
        assert 0 < len(block.text) <= 512
    finally:
        await server.aclose()


async def test_list_tools_enforces_catalog_bound(
    mock_mcp: MockMCPHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_mod, "MAX_TOOLS", 2)
    session = UpstreamSession(streamable_target(mock_mcp), call_timeout_s=10.0)
    await session.start()
    try:
        with pytest.raises(UpstreamRemoteError, match="discovery"):
            await session.list_tools()
    finally:
        await session.aclose()


async def test_list_tools_enforces_page_bound(
    mock_mcp: MockMCPHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_mod, "MAX_TOOL_PAGES", 0)
    session = UpstreamSession(streamable_target(mock_mcp), call_timeout_s=10.0)
    await session.start()
    try:
        with pytest.raises(UpstreamRemoteError, match="discovery"):
            await session.list_tools()
    finally:
        await session.aclose()
