"""Minimal extra ASGI servers for the production-readiness regression tests.

Deliberately separate from `tests/fixtures/` (which must not change) and tiny:
a recorder endpoint, a configurable (possibly cross-origin) redirect endpoint,
a fixed-body endpoint, and a slow streaming endpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route


@dataclass
class RecordedCall:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes | None


@dataclass
class ServerHandle:
    base_url: str = ""
    calls: list[RecordedCall] = field(default_factory=list[RecordedCall])
    stop_fn: Any = field(default=None, repr=False)

    async def aclose(self) -> None:
        if self.stop_fn is not None:
            await self.stop_fn()


def _record(handle: ServerHandle, request: Request, body: bytes | None) -> None:
    handle.calls.append(
        RecordedCall(
            method=request.method,
            path=request.url.path,
            query=dict(request.query_params),
            headers={k.lower(): v for k, v in request.headers.items()},
            body=body,
        )
    )


async def _serve(app: Starlette, handle: ServerHandle) -> None:
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
            raise exc if exc is not None else RuntimeError("server exited before binding")
        await asyncio.sleep(0.005)
    port = server.servers[0].sockets[0].getsockname()[1]
    handle.base_url = f"http://127.0.0.1:{port}"

    async def stop() -> None:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError, asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    handle.stop_fn = stop


async def start_recorder(
    *,
    redirect_to: str | None = None,
    redirect_status: int = 302,
    big_body: bytes | None = None,
) -> ServerHandle:
    """A server that records every request.

    `/start` issues `redirect_to` with `redirect_status` when configured;
    any path answers with `big_body` when set, otherwise a small JSON object.
    """
    handle = ServerHandle()

    async def handler(request: Request) -> Response:
        body = await request.body() or None
        _record(handle, request, body)
        if big_body is not None:
            return Response(big_body, media_type="application/octet-stream")
        if redirect_to is not None and request.url.path == "/start":
            return RedirectResponse(url=redirect_to, status_code=redirect_status)
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route(
                "/{path:path}",
                handler,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
            )
        ]
    )
    await _serve(app, handle)
    return handle


async def start_streamer(
    *, chunks: int = 20, gap: float = 0.1, chunk: bytes = b"a"
) -> ServerHandle:
    """A server that streams `chunks` bytes, pausing `gap` seconds between each."""
    handle = ServerHandle()

    async def handler(request: Request) -> Response:
        await request.body()
        _record(handle, request, None)

        async def gen() -> AsyncIterator[bytes]:
            for _ in range(chunks):
                yield chunk
                await asyncio.sleep(gap)

        return StreamingResponse(gen(), media_type="text/plain")

    app = Starlette(routes=[Route("/{path:path}", handler, methods=["GET"])])
    await _serve(app, handle)
    return handle
