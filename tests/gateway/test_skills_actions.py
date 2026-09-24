"""Skills through the cam tool: list, load (with availability marking), read_file."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from cam.api.main import create_app
from cam.config import Settings
from cam.connections.orgs import ensure_org
from cam.connections.tokens import create_token
from cam.db.models import AuditLog, ServiceVisibility, User
from cam.db.session import create_db_engine, create_session_factory
from cam.registry.services import register_global_mcp
from cam.skills import store
from cam.skills.validation import parse_bundle
from fixtures.mock_mcp import MockMCPHandle
from gateway.helpers import McpConnection, error_payload, make_result_payload

SKILL_MD_TEMPLATE = (
    "---\n"
    "name: PNS Analytics\n"
    "description: How to run PNS queries through CAM\n"
    "requires: [global/{service_slug}]\n"
    "---\n\n"
    "Before answering, call `global/{service_slug}/run_query` with the buyer question.\n"
)


def _make_user(org_id: uuid.UUID, suffix: str, label: str) -> User:
    return User(
        org_id=org_id,
        email=f"gwskill-{label}-{suffix}@example.com",
        handle=f"gwskill{label}{suffix}",
        display_name=f"Gateway Skill {label}",
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

    task = asyncio.create_task(_run(), name="gw-skills-lifespan")
    await started.wait()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://skills.test", timeout=30.0
    ) as client:
        yield client
    stop.set()
    await task


@pytest.fixture(scope="module")
async def skill_env(
    mock_mcp: MockMCPHandle,
) -> AsyncGenerator[dict[str, Any]]:
    app = create_app()
    engine = create_db_engine(Settings().database_url)
    factory = create_session_factory(engine)
    suffix = uuid.uuid4().hex[:12]
    service_slug = f"skpns{suffix}"
    skill_md = SKILL_MD_TEMPLATE.format(service_slug=service_slug)
    async with factory() as session:
        org = await ensure_org(session)
        user_a = _make_user(org.id, suffix, "a")
        user_b = _make_user(org.id, suffix, "b")
        session.add_all([user_a, user_b])
        await session.flush()
        service, _connection = await register_global_mcp(
            session,
            slug=service_slug,
            name="PNS",
            url=mock_mcp.streamable_url,
            transport="streamable-http",
            auth_type="none",
            description="PNS mock",
        )
        await session.flush()
        service_id = service.id
        user_a_id = user_a.id
        token_a_raw, _ = await create_token(
            session, org_id=org.id, user_id=user_a.id, label="skills-a"
        )
        token_b_raw, _ = await create_token(
            session, org_id=org.id, user_id=user_b.id, label="skills-b"
        )
        session.add(ServiceVisibility(org_id=org.id, service_id=service_id, user_id=user_a_id))
        service.visibility = "restricted"
        skill, version = await store.create_skill(
            session,
            org_id=org.id,
            tier="global",
            owner_user_id=None,
            actor_user_id=user_a.id,
            slug=f"pns-skills{suffix}",
            source=parse_bundle(
                {
                    "SKILL.md": skill_md.encode(),
                    "references/schema.md": b"Column glossary for PNS results.\n",
                }
            ),
            visibility="all",
            notes=None,
        )
        assert skill.id and version.version == 1
        await session.commit()
    async with _running_app(app) as client:
        yield {
            "client": client,
            "token_a": token_a_raw,
            "token_b": token_b_raw,
            "service_slug": service_slug,
            "skill_ref": f"global/pns-skills{suffix}",
            "user": user_a,
        }
    await engine.dispose()


def _connection(skill_env: dict[str, Any], token_key: str) -> Any:
    return McpConnection(skill_env["client"], skill_env[token_key])


async def _initialized(skill_env: dict[str, Any], token_key: str) -> Any:
    connection = _connection(skill_env, token_key)
    await connection.initialize()
    return connection


async def test_list_skills_marks_unavailable_without_access(
    skill_env: dict[str, Any],
) -> None:
    connection = await _initialized(skill_env, "token_b")
    listed = make_result_payload(await connection.call({"action": "list_skills"}))
    entry = next(item for item in listed["skills"] if item["id"] == skill_env["skill_ref"])
    assert entry["name"] == "PNS Analytics"
    assert entry["description"] == "How to run PNS queries through CAM"
    assert entry["available"] is False
    assert entry["unavailable_reason"] == (
        f"requires global/{skill_env['service_slug']} (no access)"
    )


async def test_load_skill_unavailable_lists_reason(
    skill_env: dict[str, Any],
) -> None:
    connection = await _initialized(skill_env, "token_b")
    error = error_payload(
        await connection.call({"action": "load_skill", "id": skill_env["skill_ref"]})
    )
    assert error["code"] == "SKILL_UNAVAILABLE"
    assert f"requires global/{skill_env['service_slug']} (no access)" in error["message"]


async def test_load_skill_returns_latest_version_and_content(
    skill_env: dict[str, Any],
) -> None:
    connection = await _initialized(skill_env, "token_a")
    loaded = make_result_payload(
        await connection.call({"action": "load_skill", "id": skill_env["skill_ref"]})
    )
    if "id" not in loaded:
        raise AssertionError(f"got {loaded!r}")
    assert loaded["id"] == skill_env["skill_ref"]
    assert loaded["version"] == 1
    assert f"global/{skill_env['service_slug']}/run_query" in loaded["content"]
    assert loaded["files"] == ["SKILL.md", "references/schema.md"]

    read = make_result_payload(
        await connection.call(
            {
                "action": "read_skill_file",
                "id": skill_env["skill_ref"],
                "path": "references/schema.md",
            }
        )
    )
    assert read["path"] == "references/schema.md"
    assert "Column glossary" in read["content"]


async def test_read_skill_file_rejects_unknown_and_traversal(
    skill_env: dict[str, Any],
) -> None:
    connection = await _initialized(skill_env, "token_a")
    unknown = error_payload(
        await connection.call(
            {
                "action": "read_skill_file",
                "id": skill_env["skill_ref"],
                "path": "references/missing.md",
            }
        )
    )
    assert unknown["code"] == "NOT_FOUND"
    traversal = error_payload(
        await connection.call(
            {
                "action": "read_skill_file",
                "id": skill_env["skill_ref"],
                "path": "../../etc/passwd",
            }
        )
    )
    assert traversal["code"] == "NOT_FOUND"


async def test_load_unknown_skill_not_found(skill_env: dict[str, Any]) -> None:
    connection = await _initialized(skill_env, "token_a")
    error = error_payload(
        await connection.call({"action": "load_skill", "id": "global/nope-skill"})
    )
    assert error["code"] == "NOT_FOUND"


async def test_per_user_description_lists_skills(
    skill_env: dict[str, Any],
) -> None:
    connection = await _initialized(skill_env, "token_b")
    result = await connection.result("tools/list")
    cam = next(tool for tool in result["tools"] if tool["name"] == "cam")
    description = cam["description"]
    assert f"skill {skill_env['skill_ref']}: How to run PNS queries through CAM" in description


async def test_load_and_read_are_audited_with_version(
    skill_env: dict[str, Any],
) -> None:
    connection = await _initialized(skill_env, "token_a")
    make_result_payload(
        await connection.call({"action": "load_skill", "id": skill_env["skill_ref"]})
    )
    make_result_payload(
        await connection.call(
            {
                "action": "read_skill_file",
                "id": skill_env["skill_ref"],
                "path": "references/schema.md",
            }
        )
    )
    engine = create_db_engine(Settings().database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            rows = (
                await session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.org_id == skill_env["user"].org_id,
                        AuditLog.action.in_(("load_skill", "read_skill_file")),
                    )
                    .order_by(AuditLog.ts)
                )
            ).scalars()
            events = {event.action: event for event in rows}
        assert len(events) >= 2
        assert events["load_skill"].skill_version_id is not None
        assert events["read_skill_file"].skill_version_id == events["load_skill"].skill_version_id
        assert events["load_skill"].token_id is not None
    finally:
        await engine.dispose()
