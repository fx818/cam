"""End-to-end tests for the upstream MCP client against the mock fixture."""

import asyncio
import uuid

import pytest

from cam.upstream import (
    ConnectionTarget,
    ImageBlock,
    TextBlock,
    UpstreamAuthError,
    UpstreamSession,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from fixtures.mock_mcp import MockMCPHandle


def streamable_target(handle: MockMCPHandle, **overrides: object) -> ConnectionTarget:
    base: dict[str, object] = {
        "service_id": "test/mock",
        "connection_id": f"conn-{uuid.uuid4().hex[:8]}",
        "url": handle.streamable_url,
        "transport": "streamable-http",
    }
    base.update(overrides)
    return ConnectionTarget(**base)  # type: ignore[arg-type]


async def open_session(target: ConnectionTarget, **kwargs: float) -> UpstreamSession:
    session = UpstreamSession(target, call_timeout_s=kwargs.pop("call_timeout_s", 10.0))
    await session.start(**kwargs)
    return session


async def test_list_tools_returns_mock_catalog(mock_mcp: MockMCPHandle) -> None:
    session = await open_session(streamable_target(mock_mcp))
    try:
        tools = await session.list_tools()
        names = {t.name for t in tools}
        assert {"echo", "slow", "huge", "fail", "image", "structured_echo"} <= names
        echo = next(t for t in tools if t.name == "echo")
        assert "text" in echo.input_schema["properties"]
        assert echo.description
    finally:
        await session.aclose()


async def test_echo_roundtrip(fresh_counts: MockMCPHandle) -> None:
    session = await open_session(streamable_target(fresh_counts))
    try:
        outcome = await session.call_tool("echo", {"text": "ping"})
        assert outcome.is_error is False
        assert outcome.content == (TextBlock(text="ping"),)
        assert fresh_counts.call_counts["echo"] == 1
    finally:
        await session.aclose()


async def test_structured_echo_returns_structured_content(mock_mcp: MockMCPHandle) -> None:
    session = await open_session(streamable_target(mock_mcp))
    try:
        outcome = await session.call_tool("structured_echo", {"value": {"a": 1}})
        assert outcome.structured == {"echo": {"a": 1}}
        assert outcome.is_error is False
    finally:
        await session.aclose()


async def test_fail_sets_is_error(mock_mcp: MockMCPHandle) -> None:
    session = await open_session(streamable_target(mock_mcp))
    try:
        outcome = await session.call_tool("fail", {"message": "nope"})
        assert outcome.is_error is True
        assert any(isinstance(b, TextBlock) and "nope" in b.text for b in outcome.content)
    finally:
        await session.aclose()


async def test_image_tool_passthrough(mock_mcp: MockMCPHandle) -> None:
    session = await open_session(streamable_target(mock_mcp))
    try:
        outcome = await session.call_tool("image", {})
        assert any(isinstance(b, ImageBlock) for b in outcome.content)
    finally:
        await session.aclose()


async def test_huge_tool_returns_full_text(mock_mcp: MockMCPHandle) -> None:
    session = await open_session(streamable_target(mock_mcp))
    try:
        outcome = await session.call_tool("huge", {"chars": 50_000})
        assert outcome.content
        block = outcome.content[0]
        assert isinstance(block, TextBlock)
        assert len(block.text) == 50_000
    finally:
        await session.aclose()


async def test_dynamic_tool_add_emits_list_changed(fresh_counts: MockMCPHandle) -> None:
    notifications: list[object] = []

    async def handler(message: object) -> None:
        notifications.append(message)

    tool_name = f"extra_{uuid.uuid4().hex[:8]}"
    target = streamable_target(fresh_counts, message_handler=handler)
    session = await open_session(target)
    try:
        before = {t.name for t in await session.list_tools()}
        assert tool_name not in before
        await session.call_tool("add_dynamic_tool", {"name": tool_name})
        await asyncio.sleep(0.3)
        assert any(type(n).__name__ == "ToolListChangedNotification" for n in notifications)
        after = {t.name for t in await session.list_tools()}
        assert tool_name in after
        outcome = await session.call_tool(tool_name, {})
        assert outcome.is_error is False
        assert fresh_counts.call_counts[tool_name] == 1
    finally:
        await session.aclose()


async def test_call_timeout_maps_to_upstream_timeout(fresh_counts: MockMCPHandle) -> None:
    session = await open_session(streamable_target(fresh_counts))
    try:
        with pytest.raises(UpstreamTimeoutError):
            await session.call_tool("slow", {"sleep_s": 5.0}, timeout_s=0.3)
        # Never re-sent: the mock only saw one invocation.
        assert fresh_counts.call_counts["slow"] == 1
    finally:
        await session.aclose()


async def test_sse_transport_works(mock_mcp: MockMCPHandle) -> None:
    target = streamable_target(mock_mcp, url=mock_mcp.sse_url, transport="sse")
    session = await open_session(target)
    try:
        outcome = await session.call_tool("echo", {"text": "via-sse"})
        assert outcome.content == (TextBlock(text="via-sse"),)
    finally:
        await session.aclose()


async def test_auth_missing_token_maps_to_upstream_auth(mock_mcp_authed: MockMCPHandle) -> None:
    with pytest.raises(UpstreamAuthError):
        await open_session(streamable_target(mock_mcp_authed))


async def test_auth_valid_token_connects(mock_mcp_authed: MockMCPHandle) -> None:
    target = streamable_target(mock_mcp_authed, headers={"Authorization": "Bearer test-secret"})
    session = await open_session(target)
    try:
        outcome = await session.call_tool("echo", {"text": "authed"})
        assert outcome.content == (TextBlock(text="authed"),)
    finally:
        await session.aclose()


async def test_dead_endpoint_maps_to_unavailable() -> None:
    target = ConnectionTarget(
        service_id="test/dead",
        connection_id="conn-dead",
        url="http://127.0.0.1:9/mcp",
    )
    with pytest.raises(UpstreamUnavailableError):
        await open_session(target, timeout_s=2.0)


async def test_server_info_populated(mock_mcp: MockMCPHandle) -> None:
    session = await open_session(streamable_target(mock_mcp))
    try:
        # server_info is optional (2026-era servers may omit identity).
        _ = session.server_info
        assert session.healthy
    finally:
        await session.aclose()
        assert session.closed
