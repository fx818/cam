from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from cam.api.main import create_app


@asynccontextmanager
async def health_client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        yield client


async def test_health_ok() -> None:
    app = create_app()
    async with health_client(app) as client:
        response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_unreachable_db_returns_503() -> None:
    app = create_app("postgresql+asyncpg://cam:cam@127.0.0.1:59999/cam_missing")
    async with health_client(app) as client:
        response = await client.get("/api/health")
    assert response.status_code == 503
    assert response.json() == {"status": "unhealthy"}
