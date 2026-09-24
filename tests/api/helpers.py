from __future__ import annotations

import httpx
from fastapi import FastAPI

from cam.auth.sessions import CSRF_COOKIE, CSRF_HEADER
from cam.db.models import User


class ApiEnv:
    __slots__ = ("app", "client")

    def __init__(self, app: FastAPI, client: httpx.AsyncClient) -> None:
        self.app = app
        self.client = client


class UserEnv:
    __slots__ = ("app", "client", "user")

    def __init__(self, app: FastAPI, client: httpx.AsyncClient, user: User) -> None:
        self.app = app
        self.client = client
        self.user = user


async def ensure_csrf(client: httpx.AsyncClient) -> str:
    if client.cookies.get(CSRF_COOKIE) is None:
        await client.get("/api/auth/me")
    token = client.cookies.get(CSRF_COOKIE)
    assert token is not None
    return token


async def post_json(
    client: httpx.AsyncClient, path: str, body: dict[str, object] | None = None
) -> httpx.Response:
    csrf = await ensure_csrf(client)
    return await client.post(
        path, json=body if body is not None else {}, headers={CSRF_HEADER: csrf}
    )


async def mutate_json(
    client: httpx.AsyncClient, method: str, path: str, body: dict[str, object] | None = None
) -> httpx.Response:
    csrf = await ensure_csrf(client)
    return await client.request(
        method, path, json=body if body is not None else {}, headers={CSRF_HEADER: csrf}
    )


async def login(client: httpx.AsyncClient, email: str, password: str) -> httpx.Response:
    return await post_json(client, "/api/auth/login", {"email": email, "password": password})
