import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from cam.api import main as api_main
from cam.api.main import WEB_DIST, create_app

INDEX = WEB_DIST / "index.html"
requires_dist = pytest.mark.skipif(not INDEX.is_file(), reason="web/dist not built")


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        yield client


@requires_dist
async def test_spa_index_served_at_root() -> None:
    app = create_app()
    async with _client(app) as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="root"' in response.text


@requires_dist
async def test_spa_fallback_for_client_routes() -> None:
    app = create_app()
    async with _client(app) as client:
        response = await client.get("/services/global/redash")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


@requires_dist
async def test_unknown_api_path_returns_error_envelope() -> None:
    app = create_app()
    async with _client(app) as client:
        response = await client.get("/api/nope")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"


async def test_no_ui_build_leaves_api_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_main, "WEB_DIST", tmp_path)
    app = create_app()
    async with _client(app) as client:
        response = await client.get("/api/health")
        spa = await client.get("/")
    assert response.status_code == 200
    assert spa.status_code == 404


def test_openapi_export_has_health() -> None:
    app = create_app()
    schema = app.openapi()
    assert "/api/health" in schema["paths"]
    json.dumps(schema)
