"""Phase 6 done-when: a global skill loads through the cam tool with tool refs,
and a user without access to the required service sees it marked unavailable."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from cam.api.main import create_app
from cam.config import Settings
from cam.connections.orgs import ensure_org
from cam.connections.tokens import create_token
from cam.db.models import ServiceVisibility, User
from cam.db.session import create_db_engine, create_session_factory
from cam.registry.services import register_global_mcp
from cam.skills import store
from cam.skills.validation import parse_bundle
from fixtures.mock_mcp import MockMCPHandle
from gateway.helpers import McpConnection, error_payload, make_result_payload


def _user(org_id: uuid.UUID, suffix: str, label: str) -> User:
    return User(
        org_id=org_id,
        email=f"dw6-{label}-{suffix}@example.com",
        handle=f"dw6{label}{suffix}",
        display_name=f"DoneWhen6 {label}",
        password_hash="x",
        role="user",
        kind="person",
    )


@asynccontextmanager
async def _running_app(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    stop = asyncio.Event()
    started = asyncio.Event()

    async def _run() -> None:
        try:
            async with app.router.lifespan_context(app):
                started.set()
                await stop.wait()
        except BaseException:
            started.set()
            raise

    task = asyncio.create_task(_run(), name="dw6-lifespan")
    await started.wait()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://dw6.test", timeout=30.0
    ) as client:
        yield client
    stop.set()
    await task


@pytest.fixture(scope="module")
async def dw6(mock_mcp: MockMCPHandle) -> AsyncGenerator[dict[str, Any]]:
    app = create_app()
    engine = create_db_engine(Settings().database_url)
    factory = create_session_factory(engine)
    suffix = uuid.uuid4().hex[:12]
    service_slug = f"pns{suffix}"
    skill_md = (
        "---\n"
        "name: PNS Analytics\n"
        "description: How to query PNS through CAM\n"
        f"requires: [global/{service_slug}]\n"
        "---\n\n"
        f"Use the tool `global/{service_slug}/run_query` for every buyer question.\n"
    )
    async with factory() as session:
        org = await ensure_org(session)
        allowed = _user(org.id, suffix, "a")
        denied = _user(org.id, suffix, "b")
        session.add_all([allowed, denied])
        await session.flush()
        allowed_id, denied_id = allowed.id, denied.id
        service, _connection = await register_global_mcp(
            session,
            slug=service_slug,
            name="PNS",
            url=mock_mcp.streamable_url,
            transport="streamable-http",
            auth_type="none",
            description="PNS mock",
        )
        await store.create_skill(
            session,
            org_id=org.id,
            tier="global",
            owner_user_id=None,
            actor_user_id=allowed_id,
            slug="pns-analytics",
            source=parse_bundle(
                {
                    "SKILL.md": skill_md.encode(),
                    "references/columns.csv": b"column,meaning\nmcGuid,call id\n",
                }
            ),
            visibility="all",
            notes=None,
        )
        token_raw, _ = await create_token(session, org_id=org.id, user_id=allowed_id, label="dw6-a")
        token_b_raw, _ = await create_token(
            session, org_id=org.id, user_id=denied_id, label="dw6-b"
        )
        session.add(ServiceVisibility(org_id=org.id, service_id=service.id, user_id=allowed_id))
        service.visibility = "restricted"
        await session.commit()
    async with _running_app(app) as client:
        yield {
            "client": client,
            "token": token_raw,
            "token_denied": token_b_raw,
            "service_slug": service_slug,
        }


async def _initialized(client: httpx.AsyncClient, token: str) -> McpConnection:
    connection = McpConnection(client, token)
    await connection.initialize()
    return connection


async def test_phase6_done_when(dw6: dict[str, Any]) -> None:
    allowed = await _initialized(dw6["client"], dw6["token"])
    denied = await _initialized(dw6["client"], dw6["token_denied"])

    listed = make_result_payload(await denied.call({"action": "list_skills"}))
    entry = next(item for item in listed["skills"] if item["id"] == "global/pns-analytics")
    assert entry["author"] is None
    assert entry["available"] is False
    assert entry["unavailable_reason"] == (f"requires global/{dw6['service_slug']} (no access)")

    allowed_listed = make_result_payload(await allowed.call({"action": "list_skills"}))
    allowed_entry = next(
        item for item in allowed_listed["skills"] if item["id"] == "global/pns-analytics"
    )
    assert allowed_entry["available"] is True
    assert allowed_entry["unavailable_reason"] is None

    error = error_payload(await denied.call({"action": "load_skill", "id": "global/pns-analytics"}))
    assert error["code"] == "SKILL_UNAVAILABLE"
    assert f"requires global/{dw6['service_slug']} (no access)" in error["message"]

    loaded = make_result_payload(
        await allowed.call({"action": "load_skill", "id": "global/pns-analytics"})
    )
    assert loaded["version"] == 1
    assert f"global/{dw6['service_slug']}/run_query" in loaded["content"]
