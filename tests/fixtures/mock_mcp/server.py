"""Mock MCP server fixture for CAM tests.

Provides both Streamable HTTP and SSE endpoints with the tools the upstream
client tests need: echo, slow, huge, fail (isError), image, structured echo
and a dynamic tool that emits `notifications/tools/list_changed`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import Counter
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image
from starlette.applications import Starlette

# 1x1 transparent PNG.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


class _RequireBearer:
    """Plain ASGI guard: 401 unless `Authorization: Bearer <token>` matches."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode("latin-1")
            if auth != f"Bearer {self.token}":
                body = b"Unauthorized"
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"text/plain"),
                            (b"content-length", str(len(body)).encode("ascii")),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)  # type: ignore[operator]


def _build_server(call_counts: Counter[str]) -> MCPServer:
    server = MCPServer(name="cam-mock-mcp", instructions="Mock MCP server for CAM tests.")

    @server.tool(description="Echo text back unchanged")
    def echo(text: str) -> str:
        call_counts["echo"] += 1
        return text

    @server.tool(description="Sleep for sleep_s seconds, then return text")
    async def slow(sleep_s: float = 5.0, text: str = "done") -> str:
        call_counts["slow"] += 1
        await asyncio.sleep(sleep_s)
        return text

    @server.tool(description="Return a large string (or JSON array) of about chars characters")
    def huge(chars: int = 100_000, as_json: bool = False) -> str:
        call_counts["huge"] += 1
        if as_json:
            import json

            items = [{"i": i, "pad": "x" * 20} for i in range(max(1, chars // 25))]
            return json.dumps(items)
        return "x" * max(1, chars)

    @server.tool(description="Always fails with an isError tool result")
    def fail(message: str = "boom") -> str:
        call_counts["fail"] += 1
        raise ToolError(message)

    @server.tool(description="Return a 1x1 PNG image")
    def image() -> Image:
        call_counts["image"] += 1
        return Image(data=_PNG_BYTES, format="png")

    @server.tool(description="Echo the value object as structured content")
    def structured_echo(value: dict[str, Any]) -> dict[str, Any]:
        call_counts["structured_echo"] += 1
        return {"echo": value}

    @server.tool(description="Register a new tool and emit list_changed")
    async def add_dynamic_tool(name: str, ctx: Context[None]) -> str:
        call_counts["add_dynamic_tool"] += 1

        def dynamic(payload: str = "dynamic") -> str:
            call_counts[name] += 1
            return payload

        server.add_tool(dynamic, name=name, description="Dynamically added tool")
        await ctx.session.send_tool_list_changed()
        return name

    return server


@dataclass(slots=True)
class MockMCPHandle:
    """Running mock MCP server: both transports, call counters, helpers."""

    streamable_url: str
    sse_url: str
    call_counts: Counter[str]
    add_tool: Callable[[str], None] = field(repr=False)
    stop: Callable[[], Any] = field(repr=False)
    require_token: str | None = None

    async def aclose(self) -> None:
        await self.stop()


async def _serve(
    app: Starlette, ready: asyncio.Future[int]
) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        lifespan="on",
        timeout_graceful_shutdown=0,
    )
    server = uvicorn.Server(config)
    task: asyncio.Task[None] = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            exc = task.exception()
            if exc is not None:
                raise exc
            raise RuntimeError("uvicorn exited before binding")
        await asyncio.sleep(0.005)
    assert server.servers
    port = server.servers[0].sockets[0].getsockname()[1]
    if not ready.done():
        ready.set_result(port)
    return server, task


async def _stop_server(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5)
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    except asyncio.CancelledError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _maybe_guard(app: Any, token: str | None) -> Any:
    if token is None:
        return app
    return _RequireBearer(app, token)


@asynccontextmanager
async def start_mock_mcp(*, require_token: str | None = None) -> AsyncGenerator[MockMCPHandle]:
    """Start streamable-http + SSE mock MCP servers on ephemeral ports."""
    call_counts: Counter[str] = Counter()
    server = _build_server(call_counts)
    dynamic_names: list[str] = []

    def add_tool(name: str) -> None:
        dynamic_names.append(name)

    streamable_inner = server.streamable_http_app(streamable_http_path="/mcp")
    sse_inner = server.sse_app()
    streamable_app = _maybe_guard(streamable_inner, require_token)
    sse_app = _maybe_guard(sse_inner, require_token)

    loop = asyncio.get_running_loop()
    streamable_ready: asyncio.Future[int] = loop.create_future()
    sse_ready: asyncio.Future[int] = loop.create_future()
    streamable_server, streamable_task = await _serve(streamable_app, streamable_ready)  # type: ignore[arg-type]
    sse_server, sse_task = await _serve(sse_app, sse_ready)  # type: ignore[arg-type]
    streamable_port = await streamable_ready
    sse_port = await sse_ready

    async def stop() -> None:
        await _stop_server(streamable_server, streamable_task)
        await _stop_server(sse_server, sse_task)

    handle = MockMCPHandle(
        streamable_url=f"http://127.0.0.1:{streamable_port}/mcp",
        sse_url=f"http://127.0.0.1:{sse_port}/sse",
        call_counts=call_counts,
        add_tool=add_tool,
        stop=stop,
        require_token=require_token,
    )
    try:
        yield handle
    finally:
        await stop()
