from __future__ import annotations

import hmac
from typing import cast

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cam.auth.sessions import CSRF_COOKIE, CSRF_HEADER, new_csrf_token

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_FORBIDDEN_BODY = b'{"error":{"code":"FORBIDDEN","message":"Missing or invalid CSRF token."}}'


def _headers(scope: Scope) -> list[tuple[bytes, bytes]]:
    return cast("list[tuple[bytes, bytes]]", scope.get("headers") or [])


def _cookie_header(scope: Scope) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for name, value in _headers(scope):
        if name.lower() == b"cookie":
            for part in value.decode("latin-1").split("; "):
                key, sep, val = part.partition("=")
                if sep:
                    cookies[key] = val
    return cookies


def _request_header(scope: Scope, name: bytes) -> str | None:
    lowered = name.lower()
    for header_name, value in _headers(scope):
        if header_name.lower() == lowered:
            return value.decode("latin-1")
    return None


async def _send_forbidden(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_FORBIDDEN_BODY)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _FORBIDDEN_BODY})


def _set_cookie_attributes(scope: Scope) -> str:
    attributes = "Path=/; SameSite=Lax"
    if scope.get("scheme") == "https":
        attributes += "; Secure"
    return attributes


class CsrfMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not path.startswith("/api"):
            await self.app(scope, receive, send)
            return
        cookies = _cookie_header(scope)
        csrf_cookie = cookies.get(CSRF_COOKIE)
        method = scope.get("method", "")
        if method in MUTATING_METHODS:
            header = _request_header(scope, CSRF_HEADER.encode("latin-1"))
            if not csrf_cookie or not header or not hmac.compare_digest(csrf_cookie, header):
                await _send_forbidden(send)
                return
        if csrf_cookie is None:
            token = new_csrf_token()
            attributes = _set_cookie_attributes(scope)
            cookie_value = f"{CSRF_COOKIE}={token}; {attributes}".encode("latin-1")

            async def send_with_cookie(message: Message) -> None:
                if message["type"] == "http.response.start":
                    existing = cast("list[tuple[bytes, bytes]]", message.get("headers") or [])
                    message["headers"] = [*existing, (b"set-cookie", cookie_value)]
                await send(message)

            await self.app(scope, receive, send_with_cookie)
            return
        await self.app(scope, receive, send)
