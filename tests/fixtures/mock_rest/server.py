"""Mock REST API server fixture for CAM tests.

Serves the bundled OpenAPI spec plus the endpoints it describes. Requests are
authorized with an API key header, HTTP basic, or a bearer token from the
shared `bearer_tokens` set (populated by the mock OAuth fixture). Every
request is recorded on the handle so tests can assert which credential CAM
injected. When an OAuth issuer is set, RFC 9728 protected-resource metadata is
served for MCP-spec OAuth discovery.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections import Counter
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from fixtures.mock_rest.spec import OPENAPI_SPEC

API_KEY_NAME = "X-API-Key"
API_KEY_VALUE = "rest-key"
BASIC_USER = "restuser"
BASIC_PASSWORD = "restpass"

DEFAULT_ITEMS: list[dict[str, Any]] = [
    {"id": "i-1", "name": "alpha", "tags": ["x"]},
    {"id": "i-2", "name": "beta", "tags": ["y", "z"]},
    {"id": "i-3", "name": "gamma", "tags": []},
]


@dataclass(slots=True)
class RecordedRequest:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: Any


@dataclass(slots=True)
class MockRestHandle:
    base_url: str
    spec_url: str
    spec: dict[str, Any]
    requests: list[RecordedRequest] = field(default_factory=list[RecordedRequest])
    call_counts: Counter[str] = field(default_factory=Counter[str])
    bearer_tokens: set[str] = field(default_factory=set[str])
    items: list[dict[str, Any]] = field(
        default_factory=lambda: [dict(item) for item in DEFAULT_ITEMS]
    )
    oauth_issuer: str | None = None
    stop: Any = field(repr=False, default=lambda: None)

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


def _auth_method(request: Request, bearer_tokens: set[str]) -> str | None:
    api_key = request.headers.get(API_KEY_NAME.lower())
    if api_key == API_KEY_VALUE:
        return "api-key"
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "basic" and value:
        try:
            decoded = base64.b64decode(value).decode("utf-8")
        except Exception:
            return None
        user, _, password = decoded.partition(":")
        if user == BASIC_USER and password == BASIC_PASSWORD:
            return "basic"
        return None
    if scheme.lower() == "bearer" and value:
        if value in bearer_tokens:
            return "bearer"
        return None
    return None


def _record(handle: MockRestHandle, request: Request, body: Any) -> None:
    handle.requests.append(
        RecordedRequest(
            method=request.method,
            path=request.url.path,
            query=dict(request.query_params),
            headers={k.lower(): v for k, v in request.headers.items()},
            body=body,
        )
    )


async def _read_body(request: Request) -> Any:
    raw = await request.body()
    if not raw:
        return None
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw.decode("utf-8", errors="replace")


def _build_app(handle: MockRestHandle) -> Starlette:
    def _unauthorized() -> Response:
        return Response("Unauthorized", status_code=401, media_type="text/plain")

    async def openapi_json(_request: Request) -> Response:
        return JSONResponse(handle.spec)

    async def items(request: Request) -> Response:
        _record(handle, request, await _read_body(request))
        if _auth_method(request, handle.bearer_tokens) is None:
            return _unauthorized()
        handle.call_counts["items"] += 1
        if request.method == "POST":
            payload = cast("dict[str, Any]", handle.requests[-1].body or {})
            tags_value = payload.get("tags")
            created: dict[str, Any] = {
                "id": f"i-{len(handle.items) + 1}",
                "name": str(payload.get("name") or ""),
                "tags": tags_value if isinstance(tags_value, list) else [],
            }
            handle.items.append(created)
            return JSONResponse(created, status_code=201)
        q = request.query_params.get("q")
        rows = handle.items
        if q:
            rows = [row for row in rows if q in str(row.get("name", ""))]
        limit = request.query_params.get("limit")
        if limit and limit.isdigit():
            rows = rows[: int(limit)]
        return JSONResponse(rows)

    async def item_detail(request: Request) -> Response:
        _record(handle, request, await _read_body(request))
        if _auth_method(request, handle.bearer_tokens) is None:
            return _unauthorized()
        handle.call_counts["item"] += 1
        item_id = request.path_params["item_id"]
        for row in handle.items:
            if row["id"] == item_id:
                return JSONResponse(row)
        return Response(f"item {item_id} not found", status_code=404, media_type="text/plain")

    async def whoami(request: Request) -> Response:
        _record(handle, request, await _read_body(request))
        method = _auth_method(request, handle.bearer_tokens)
        if method is None:
            return _unauthorized()
        handle.call_counts["whoami"] += 1
        return JSONResponse({"auth": method, "path": request.url.path})

    async def plain(request: Request) -> Response:
        _record(handle, request, await _read_body(request))
        if _auth_method(request, handle.bearer_tokens) is None:
            return _unauthorized()
        handle.call_counts["plain"] += 1
        return PlainTextResponse("hello from mock rest")

    async def missing(request: Request) -> Response:
        _record(handle, request, await _read_body(request))
        return Response("no such thing", status_code=404, media_type="text/plain")

    async def boom(request: Request) -> Response:
        _record(handle, request, await _read_body(request))
        return Response("internal failure", status_code=500, media_type="text/plain")

    async def throttled(request: Request) -> Response:
        _record(handle, request, await _read_body(request))
        return Response("slow down", status_code=429, media_type="text/plain")

    async def slow(request: Request) -> Response:
        _record(handle, request, await _read_body(request))
        raw = request.query_params.get("seconds", "1")
        try:
            seconds = float(raw)
        except ValueError:
            seconds = 1.0
        await asyncio.sleep(seconds)
        return JSONResponse({"slept": seconds})

    async def redirect(request: Request) -> Response:
        _record(handle, request, await _read_body(request))
        return RedirectResponse(url="/items", status_code=302)

    async def open_endpoint(request: Request) -> Response:
        _record(handle, request, await _read_body(request))
        return JSONResponse({"status": "pong"})

    async def protected_resource(request: Request) -> Response:
        if handle.oauth_issuer is None:
            return Response("Not found", status_code=404)
        return JSONResponse(
            {
                "resource": handle.base_url,
                "authorization_servers": [handle.oauth_issuer],
                "scopes_supported": ["mock.read", "mock.write"],
                "bearer_methods_supported": ["header"],
            }
        )

    routes = [
        Route("/openapi.json", openapi_json, methods=["GET"]),
        Route("/items", items, methods=["GET", "POST"]),
        Route("/items/{item_id}", item_detail, methods=["GET"]),
        Route("/whoami", whoami, methods=["GET"]),
        Route("/plain", plain, methods=["GET"]),
        Route("/missing", missing, methods=["GET"]),
        Route("/boom", boom, methods=["GET"]),
        Route("/throttled", throttled, methods=["GET"]),
        Route("/slow", slow, methods=["GET"]),
        Route("/redirect", redirect, methods=["GET"]),
        Route("/open", open_endpoint, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", protected_resource, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/", protected_resource, methods=["GET"]),
    ]
    return Starlette(routes=routes)


@asynccontextmanager
async def start_mock_rest() -> AsyncGenerator[MockRestHandle]:
    handle = MockRestHandle(base_url="", spec_url="", spec=OPENAPI_SPEC)
    app = _build_app(handle)
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[int] = loop.create_future()
    server, task = await _serve(app, ready)
    port = await ready
    handle.base_url = f"http://127.0.0.1:{port}"
    handle.spec_url = f"{handle.base_url}/openapi.json"

    async def stop() -> None:
        await _stop_server(server, task)

    handle.stop = stop
    try:
        yield handle
    finally:
        await stop()
