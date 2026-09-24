"""Bearer-token authentication for the /mcp endpoint.

Every HTTP request must carry `Authorization: Bearer cam_<token>`. A missing,
revoked or unknown token gets HTTP 401 before any MCP session starts. On
success the identity is placed on the ASGI scope for the MCP layer, and the
session-manager principal is bound to the CAM token so a session cannot be
replayed with a different credential.
"""

from __future__ import annotations

import json
from collections.abc import MutableMapping
from typing import Any

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp, Receive, Scope, Send

from cam.connections.tokens import AuthenticatedIdentity, authenticate_token, mark_token_used

UNAUTHORIZED_BODY = json.dumps(
    {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "Invalid or expired CAM token. Generate a new one in the dashboard.",
        }
    }
).encode("utf-8")


def extract_bearer_token(headers: list[tuple[bytes, bytes]]) -> str | None:
    for name, value in headers:
        if name.lower() == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and token:
                return token
    return None


async def _send_unauthorized(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(UNAUTHORIZED_BODY)).encode("ascii")),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": UNAUTHORIZED_BODY})


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.app = app
        self._session_factory = session_factory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw_token = extract_bearer_token(scope.get("headers") or [])
        identity: AuthenticatedIdentity | None = None
        if raw_token is not None:
            async with self._session_factory() as session:
                identity = await authenticate_token(session, raw_token)
                if identity is not None:
                    await mark_token_used(session, identity.token)
                    await session.commit()
        if identity is None or raw_token is None:
            await _send_unauthorized(send)
            return
        mutable: MutableMapping[str, Any] = scope
        mutable["cam_identity"] = identity
        mutable["user"] = AuthenticatedUser(
            AccessToken(token=raw_token, client_id=str(identity.token.id), scopes=[])
        )
        await self.app(scope, receive, send)
