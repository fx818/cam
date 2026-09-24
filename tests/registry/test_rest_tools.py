"""Manual REST tool definition: binding validation, dry run, upsert."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.connections.orgs import ensure_org
from cam.db.models import Service, Tool
from cam.registry.rest_tools import RestToolError, define_rest_tool

_MOCK_URL = "http://127.0.0.1:9"


async def _rest_service(session: AsyncSession, *, kind: str = "rest") -> Service:
    org = await ensure_org(session)
    service = Service(
        org_id=org.id,
        tier="global",
        owner_user_id=None,
        slug=f"manual-{uuid.uuid4().hex[:10]}",
        name="Manual Target",
        kind=kind,
        url=_MOCK_URL,
        transport=None if kind == "rest" else "streamable-http",
        auth_type="none",
        auth_config={},
        visibility="all",
        status="active",
        health="unknown",
    )
    session.add(service)
    await session.flush()
    return service


async def test_define_tool_derives_schema_and_binding(db_session: AsyncSession) -> None:
    service = await _rest_service(db_session)
    tool = await define_rest_tool(
        db_session,
        service=service,
        name="getItem",
        method="get",
        path="/items/{item_id}",
        params={"item_id": "path", "verbose": "query"},
        description="Fetch one item",
    )
    await db_session.commit()
    assert tool.rest_binding == {
        "method": "GET",
        "path": "/items/{item_id}",
        "params": {"item_id": "path", "verbose": "query"},
    }
    assert tool.input_schema == {
        "type": "object",
        "properties": {"item_id": {"type": "string"}, "verbose": {"type": "string"}},
        "required": ["item_id"],
    }
    assert tool.description == "Fetch one item"
    assert tool.enabled is True
    assert tool.status == "active"


async def test_define_tool_defaults_description(db_session: AsyncSession) -> None:
    service = await _rest_service(db_session)
    tool = await define_rest_tool(
        db_session, service=service, name="ping", method="post", path="/ping"
    )
    await db_session.commit()
    assert tool.description == "POST /ping"
    assert tool.rest_binding == {"method": "POST", "path": "/ping", "params": {}}
    assert tool.input_schema == {"type": "object", "properties": {}}


async def test_define_tool_upserts_by_name(db_session: AsyncSession) -> None:
    service = await _rest_service(db_session)
    first = await define_rest_tool(
        db_session,
        service=service,
        name="listItems",
        method="get",
        path="/items",
        params={"q": "query"},
    )
    await db_session.commit()
    again = await define_rest_tool(
        db_session,
        service=service,
        name="listItems",
        method="get",
        path="/items",
        params={"q": "query", "limit": "query"},
        description="List with limit",
    )
    await db_session.commit()
    assert again.id == first.id
    rows = (
        (await db_session.execute(select(Tool).where(Tool.service_id == service.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].rest_binding is not None
    assert set(rows[0].rest_binding["params"]) == {"q", "limit"}
    assert rows[0].description == "List with limit"


async def test_define_tool_accepts_schema_override(db_session: AsyncSession) -> None:
    service = await _rest_service(db_session)
    schema = {
        "type": "object",
        "properties": {"item_id": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["item_id"],
    }
    tool = await define_rest_tool(
        db_session,
        service=service,
        name="getItem",
        method="get",
        path="/items/{item_id}",
        params={"item_id": "path", "limit": "query"},
        input_schema=schema,
    )
    await db_session.commit()
    assert tool.input_schema == schema


async def test_define_tool_rejects_invalid_definitions(db_session: AsyncSession) -> None:
    service = await _rest_service(db_session)

    invalid: list[tuple[str, str, str, dict[str, str], dict[str, object] | None]] = [
        ("bad name", "GET", "/a", {}, None),
        ("ok", "TRACE", "/a", {}, None),
        ("ok", "GET", "a", {}, None),
        ("ok", "GET", "http://x/a", {}, None),
        ("ok", "GET", "/a/{id}", {}, None),
        ("ok", "GET", "/a", {"id": "path"}, None),
        ("ok", "GET", "/a", {"x": "cookie"}, None),
        ("ok", "GET", "/a/{id}", {"id": "query"}, None),
        ("ok", "GET", "/a/{id}", {"id": "path"}, {"type": "array"}),
        ("ok", "GET", "/a/{id}", {"id": "path"}, {"type": "object", "properties": {}}),
    ]
    for tool_name, method, path, params, schema in invalid:
        with pytest.raises(RestToolError):
            await define_rest_tool(
                db_session,
                service=service,
                name=tool_name,
                method=method,
                path=path,
                params=params,
                input_schema=schema,
            )
    await db_session.rollback()


async def test_define_tool_requires_rest_service(db_session: AsyncSession) -> None:
    service = await _rest_service(db_session, kind="mcp")
    with pytest.raises(RestToolError, match="kind 'rest'"):
        await define_rest_tool(db_session, service=service, name="ping", method="get", path="/ping")


async def test_define_tool_dry_run_catches_unterminated_placeholder(
    db_session: AsyncSession,
) -> None:
    service = await _rest_service(db_session)
    with pytest.raises(RestToolError, match="invalid binding"):
        await define_rest_tool(
            db_session,
            service=service,
            name="broken",
            method="GET",
            path="/a/{",
            params={},
        )


async def test_define_tool_dry_run_catches_malformed_placeholder(
    db_session: AsyncSession,
) -> None:
    service = await _rest_service(db_session)
    with pytest.raises(RestToolError, match="invalid binding"):
        await define_rest_tool(
            db_session,
            service=service,
            name="broken",
            method="GET",
            path="/a/{id}}",
            params={"id": "path"},
        )
