"""HTTP helpers for API tests: future-proof CSRF plus bearer auth."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI

CSRF_COOKIE = "cam_csrf"
CSRF_HEADER = "X-CSRF-Token"


class ApiEnv:
    __slots__ = ("app", "client")

    def __init__(self, app: FastAPI, client: httpx.AsyncClient) -> None:
        self.app = app
        self.client = client


@asynccontextmanager
async def _run_lifespan(app: FastAPI) -> AsyncGenerator[None]:
    stop = asyncio.Event()
    started = asyncio.Event()
    failure: list[BaseException] = []

    async def _lifespan() -> None:
        try:
            async with app.router.lifespan_context(app):
                started.set()
                await stop.wait()
        except BaseException as exc:
            failure.append(exc)
            started.set()
            raise

    task = asyncio.create_task(_lifespan(), name="cam-api-test-lifespan")
    await started.wait()
    if failure:
        await task
    try:
        yield
    finally:
        stop.set()
        await task


@asynccontextmanager
async def api_client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    async with _run_lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test", timeout=30.0
        ) as client:
            yield client


async def ensure_csrf(client: httpx.AsyncClient) -> str | None:
    if client.cookies.get(CSRF_COOKIE) is None:
        await client.get("/api/health")
    return client.cookies.get(CSRF_COOKIE)


async def auth_headers(client: httpx.AsyncClient, token: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    csrf = await ensure_csrf(client)
    if csrf is not None:
        headers[CSRF_HEADER] = csrf
    return headers


async def post_json(
    client: httpx.AsyncClient, path: str, body: dict[str, Any], *, token: str
) -> httpx.Response:
    headers = await auth_headers(client, token)
    return await client.post(path, json=body, headers=headers)
