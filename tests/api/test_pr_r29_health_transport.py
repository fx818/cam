"""R29: dependency-free liveness and prod DNS-rebinding (host/origin) protection."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from cam.api.main import create_app, transport_security_settings
from cam.config import Settings

BAD_DB = "postgresql+asyncpg://cam:cam@127.0.0.1:59999/nope"


@asynccontextmanager
async def _app_client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        yield client


def _prod_settings() -> Settings:
    return Settings(
        env="prod",
        database_url=BAD_DB,
        encryption_keys="v1:AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
        encryption_key_current="v1",
        session_secret="s" * 32,
        base_url="https://cam.example.internal",
    )


def test_transport_security_dev_is_permissive() -> None:
    assert transport_security_settings(Settings()).enable_dns_rebinding_protection is False


def test_transport_security_prod_enabled_with_base_url_host() -> None:
    settings = transport_security_settings(_prod_settings())
    assert settings.enable_dns_rebinding_protection is True
    assert "cam.example.internal" in settings.allowed_hosts
    assert "cam.example.internal:*" in settings.allowed_hosts
    assert "https://cam.example.internal" in settings.allowed_origins


async def test_liveness_does_not_depend_on_database() -> None:
    app = create_app(Settings(database_url=BAD_DB))
    async with _app_client(app) as client:
        live = await client.get("/api/health/live")
        ready = await client.get("/api/health/ready")
        readiness = await client.get("/api/health")
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 503
    assert readiness.status_code == 503
