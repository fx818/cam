"""Mock OAuth authorization server for CAM tests.

Implements RFC 8414 metadata, RFC 7591 dynamic client registration, the
authorize endpoint (PKCE S256) and the token endpoint (authorization code +
refresh). Access tokens issued for `as_user` are added to a shared
`bearer_tokens` set so the mock REST API accepts them.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route


@dataclass(slots=True)
class MockOAuthHandle:
    base_url: str
    issuer: str
    bearer_tokens: set[str]
    client_id: str = "cam-mock-client"
    client_secret: str = "cam-mock-secret"
    codes: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    refresh_tokens: dict[str, str] = field(default_factory=dict[str, str])
    set_refresh_fail: bool = False
    registration_count: int = 0
    token_requests: int = 0
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


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def _form(request: Request) -> dict[str, str]:
    raw = await request.body()
    parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def _build_app(handle: MockOAuthHandle) -> Starlette:
    async def metadata(_request: Request) -> Response:
        return JSONResponse(
            {
                "issuer": handle.issuer,
                "authorization_endpoint": f"{handle.base_url}/authorize",
                "token_endpoint": f"{handle.base_url}/token",
                "registration_endpoint": f"{handle.base_url}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "scopes_supported": ["mock.read", "mock.write"],
                "token_endpoint_auth_methods_supported": [
                    "client_secret_basic",
                    "client_secret_post",
                ],
            }
        )

    async def register(request: Request) -> Response:
        handle.registration_count += 1
        return JSONResponse(
            {
                "client_id": handle.client_id,
                "client_secret": handle.client_secret,
                "redirect_uris": [],
                "token_endpoint_auth_method": "client_secret_basic",
            },
            status_code=201,
        )

    async def authorize(request: Request) -> Response:
        params = request.query_params
        redirect_uri = params.get("redirect_uri") or ""
        state = params.get("state") or ""
        challenge = params.get("code_challenge") or ""
        method = params.get("code_challenge_method") or ""
        if params.get("response_type") != "code":
            return Response("unsupported response_type", status_code=400)
        if not redirect_uri or not state or not challenge or method != "S256":
            return Response("missing PKCE parameters", status_code=400)
        code = secrets.token_urlsafe(24)
        as_user = params.get("as_user") or "alice"
        handle.codes[code] = {
            "challenge": challenge,
            "redirect_uri": redirect_uri,
            "client_id": params.get("client_id") or handle.client_id,
            "as_user": as_user,
        }
        parsed = urlparse(redirect_uri)
        query = parse_qs(parsed.query)
        query["code"] = [code]
        query["state"] = [state]
        flat = [(key, values[-1]) for key, values in query.items()]
        location = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{urlencode(flat)}"
        return RedirectResponse(url=location, status_code=302)

    async def token(request: Request) -> Response:
        form = await _form(request)
        handle.token_requests += 1
        grant_type = form.get("grant_type") or ""
        if grant_type == "authorization_code":
            code = form.get("code") or ""
            entry = handle.codes.pop(code, None)
            if entry is None:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            verifier = form.get("code_verifier") or ""
            if not verifier or _pkce_challenge(verifier) != entry["challenge"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if form.get("redirect_uri") != entry["redirect_uri"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            as_user = entry["as_user"]
            access = f"tok-{as_user}-{secrets.token_hex(4)}"
            refresh = f"rt-{as_user}-{secrets.token_hex(4)}"
            handle.bearer_tokens.add(access)
            handle.refresh_tokens[refresh] = as_user
            return JSONResponse(
                {
                    "access_token": access,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": refresh,
                }
            )
        if grant_type == "refresh_token":
            if handle.set_refresh_fail:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            refresh = form.get("refresh_token") or ""
            as_user = handle.refresh_tokens.get(refresh)
            if as_user is None:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            access = f"tok-{as_user}-{secrets.token_hex(4)}"
            new_refresh = f"rt-{as_user}-{secrets.token_hex(4)}"
            handle.bearer_tokens.add(access)
            del handle.refresh_tokens[refresh]
            handle.refresh_tokens[new_refresh] = as_user
            return JSONResponse(
                {
                    "access_token": access,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": new_refresh,
                }
            )
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    routes = [
        Route("/.well-known/oauth-authorization-server", metadata, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server/", metadata, methods=["GET"]),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize, methods=["GET"]),
        Route("/token", token, methods=["POST"]),
    ]
    return Starlette(routes=routes)


@asynccontextmanager
async def start_mock_oauth(bearer_tokens: set[str]) -> AsyncGenerator[MockOAuthHandle]:
    handle = MockOAuthHandle(base_url="", issuer="", bearer_tokens=bearer_tokens)
    app = _build_app(handle)
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[int] = loop.create_future()
    server, task = await _serve(app, ready)
    port = await ready
    handle.base_url = f"http://127.0.0.1:{port}"
    handle.issuer = handle.base_url

    async def stop() -> None:
        await _stop_server(server, task)

    handle.stop = stop
    try:
        yield handle
    finally:
        await stop()
