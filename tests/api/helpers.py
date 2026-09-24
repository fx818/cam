from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from cam.auth.sessions import CSRF_COOKIE, CSRF_HEADER
from cam.db.models import AuditLog, User


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


@asynccontextmanager
async def user_client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://api.test", timeout=30.0
    ) as client:
        yield client


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


async def insert_audit(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    owner_user_id: uuid.UUID | None = None,
    service_id: uuid.UUID | None = None,
    token_id: uuid.UUID | None = None,
    agent_name: str | None = None,
    action: str = "invoke",
    status: str = "ok",
    args: dict[str, object] | None = None,
    ts: datetime | None = None,
) -> AuditLog:
    row = AuditLog(
        org_id=org_id,
        user_id=user_id,
        owner_user_id=owner_user_id,
        service_id=service_id,
        token_id=token_id,
        agent_name=agent_name,
        action=action,
        status=status,
        args=args,
    )
    if ts is not None:
        row.ts = ts
    db.add(row)
    await db.flush()
    return row
