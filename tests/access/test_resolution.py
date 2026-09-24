"""Tests for the access-resolution boundary (real Postgres rows, no MCP)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cam.access import (
    ServiceDisabled,
    ServiceNotFound,
    list_services,
    list_tools,
    parse_service_ref,
    parse_tool_id,
    resolve_org_connection,
    resolve_service,
    resolve_tool,
)
from cam.connections.orgs import ensure_org
from cam.connections.tokens import AuthenticatedIdentity, create_token
from cam.db.models import Connection, Service, ServiceVisibility, Tool, User


async def _make_identity(db_session: AsyncSession, suffix: str) -> AuthenticatedIdentity:
    org = await ensure_org(db_session)
    user = User(
        org_id=org.id,
        email=f"access-{suffix}@example.com",
        handle=f"acc{suffix}",
        display_name="Access Test",
        password_hash="x",
        role="user",
        kind="person",
    )
    db_session.add(user)
    await db_session.flush()
    _raw, token = await create_token(db_session, org_id=org.id, user_id=user.id, label="access")
    return AuthenticatedIdentity(org_id=org.id, user=user, token=token)


def _unique_slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _make_service(
    db_session: AsyncSession,
    org_id: uuid.UUID,
    slug: str,
    *,
    visibility: str = "all",
    status: str = "active",
) -> Service:
    service = Service(
        org_id=org_id,
        tier="global",
        owner_user_id=None,
        slug=slug,
        name=slug,
        description=f"service {slug}",
        kind="mcp",
        url="http://127.0.0.1:9/mcp",
        transport="streamable-http",
        auth_type="none",
        visibility=visibility,
        status=status,
    )
    db_session.add(service)
    await db_session.flush()
    return service


async def _make_tool(
    db_session: AsyncSession,
    org_id: uuid.UUID,
    service: Service,
    name: str,
    *,
    enabled: bool = True,
    status: str = "active",
) -> Tool:
    tool = Tool(
        org_id=org_id,
        service_id=service.id,
        name=name,
        upstream_name=name,
        description=f"tool {name}",
        input_schema={"type": "object", "properties": {}},
        enabled=enabled,
        status=status,
    )
    db_session.add(tool)
    await db_session.flush()
    return tool


async def _make_connection(
    db_session: AsyncSession,
    org_id: uuid.UUID,
    service: Service,
    *,
    user_id: uuid.UUID | None = None,
    status: str = "connected",
) -> Connection:
    connection = Connection(
        org_id=org_id,
        service_id=service.id,
        user_id=user_id,
        status=status,
    )
    db_session.add(connection)
    await db_session.flush()
    return connection


async def test_list_services_visibility_matrix(db_session: AsyncSession) -> None:
    identity_a = await _make_identity(db_session, uuid.uuid4().hex[:8] + "a")
    identity_b = await _make_identity(db_session, uuid.uuid4().hex[:8] + "b")
    org_id = identity_a.org_id
    assert org_id == identity_b.org_id

    open_service = await _make_service(db_session, org_id, _unique_slug("open"))
    restricted = await _make_service(
        db_session, org_id, _unique_slug("closed"), visibility="restricted"
    )
    off = await _make_service(db_session, org_id, _unique_slug("off"), status="disabled")
    await _make_service(db_session, org_id, _unique_slug("tpl"), status="active")
    db_session.add(
        Service(
            org_id=org_id,
            tier="template",
            owner_user_id=None,
            slug=_unique_slug("tpl"),
            name="template",
            url="http://127.0.0.1:9/mcp",
            transport="streamable-http",
        )
    )
    db_session.add(
        ServiceVisibility(org_id=org_id, service_id=restricted.id, user_id=identity_b.user.id)
    )
    await db_session.flush()

    summaries_a = await list_services(db_session, identity_a)
    summaries_b = await list_services(db_session, identity_b)
    ids_a = {s.id for s in summaries_a}
    ids_b = {s.id for s in summaries_b}

    open_ref = f"global/{open_service.slug}"
    restricted_ref = f"global/{restricted.slug}"
    off_ref = f"global/{off.slug}"
    assert open_ref in ids_a
    assert open_ref in ids_b
    assert restricted_ref not in ids_a
    assert restricted_ref in ids_b
    assert off_ref not in ids_a
    assert off_ref not in ids_b
    assert all(summary.tier == "global" for summary in summaries_a)


async def test_list_services_counts_enabled_active_tools(db_session: AsyncSession) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "c")
    service = await _make_service(db_session, identity.org_id, _unique_slug("counted"))
    await _make_tool(db_session, identity.org_id, service, "on")
    await _make_tool(db_session, identity.org_id, service, "off", enabled=False)
    await _make_tool(db_session, identity.org_id, service, "gone", status="removed_upstream")

    summaries = await list_services(db_session, identity)
    summary = next(s for s in summaries if s.id == f"global/{service.slug}")
    assert summary.tool_count == 1


async def test_resolve_service_restricted_not_found_without_grant(
    db_session: AsyncSession,
) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "d")
    service = await _make_service(
        db_session, identity.org_id, _unique_slug("vault"), visibility="restricted"
    )
    ref = f"global/{service.slug}"
    with pytest.raises(ServiceNotFound):
        await resolve_service(db_session, identity, ref)

    db_session.add(
        ServiceVisibility(org_id=identity.org_id, service_id=service.id, user_id=identity.user.id)
    )
    await db_session.flush()
    resolved = await resolve_service(db_session, identity, ref)
    assert resolved.service.id == service.id
    assert resolved.ref == ref


async def test_resolve_service_disabled_status_raises(db_session: AsyncSession) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "e")
    service = await _make_service(
        db_session, identity.org_id, _unique_slug("paused"), status="disabled"
    )
    with pytest.raises(ServiceDisabled):
        await resolve_service(db_session, identity, f"global/{service.slug}")


async def test_resolve_service_rejects_bad_refs(db_session: AsyncSession) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "f")
    service = await _make_service(db_session, identity.org_id, _unique_slug("refused"))
    bad_refs = [
        "",
        "nonsense",
        "a/b/c",
        f"personal/{service.slug}",
        f"template/{service.slug}",
        f"global/{_unique_slug('unknown')}",
        f"global/{service.slug}/extra",
    ]
    for ref in bad_refs:
        with pytest.raises(ServiceNotFound):
            await resolve_service(db_session, identity, ref)


async def test_list_tools_filters_disabled(db_session: AsyncSession) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "g")
    service = await _make_service(db_session, identity.org_id, _unique_slug("filtered"))
    await _make_tool(db_session, identity.org_id, service, "alpha")
    await _make_tool(db_session, identity.org_id, service, "beta", enabled=False)
    await _make_tool(db_session, identity.org_id, service, "gamma", status="pending_review")

    resolved_service, tools = await list_tools(db_session, identity, f"global/{service.slug}")
    assert resolved_service.service.id == service.id
    assert [tool.name for tool in tools] == ["alpha"]


async def test_resolve_tool_not_found_variants(db_session: AsyncSession) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "h")
    service = await _make_service(db_session, identity.org_id, _unique_slug("variants"))
    await _make_tool(db_session, identity.org_id, service, "real")
    await _make_tool(db_session, identity.org_id, service, "disabled", enabled=False)

    with pytest.raises(ServiceNotFound):
        await resolve_tool(db_session, identity, f"global/{service.slug}/missing")
    with pytest.raises(ServiceNotFound):
        await resolve_tool(db_session, identity, f"global/{service.slug}/disabled")
    with pytest.raises(ServiceNotFound):
        await resolve_tool(db_session, identity, f"personal/{service.slug}/real")

    resolved = await resolve_tool(db_session, identity, f"global/{service.slug}/real")
    assert resolved.id == f"global/{service.slug}/real"
    assert resolved.tool.name == "real"


async def test_resolve_tool_no_access_is_not_found(db_session: AsyncSession) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "i")
    service = await _make_service(
        db_session, identity.org_id, _unique_slug("hidden"), visibility="restricted"
    )
    await _make_tool(db_session, identity.org_id, service, "secret")
    with pytest.raises(ServiceNotFound):
        await resolve_tool(db_session, identity, f"global/{service.slug}/secret")


async def test_resolve_org_connection_ok(db_session: AsyncSession) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "j")
    service = await _make_service(db_session, identity.org_id, _unique_slug("wired"))
    connection = await _make_connection(db_session, identity.org_id, service)
    resolved = await resolve_org_connection(db_session, service)
    assert resolved.id == connection.id
    assert resolved.user_id is None


async def test_resolve_org_connection_rejects_user_owned(db_session: AsyncSession) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "k")
    service = await _make_service(db_session, identity.org_id, _unique_slug("userconn"))
    await _make_connection(db_session, identity.org_id, service, user_id=identity.user.id)
    with pytest.raises(ServiceDisabled):
        await resolve_org_connection(db_session, service)


async def test_resolve_org_connection_rejects_disconnected(db_session: AsyncSession) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "m")
    service = await _make_service(db_session, identity.org_id, _unique_slug("broken"))
    await _make_connection(db_session, identity.org_id, service, status="needs_reconnect")
    with pytest.raises(ServiceDisabled):
        await resolve_org_connection(db_session, service)

    missing = await _make_service(db_session, identity.org_id, _unique_slug("unwired"))
    with pytest.raises(ServiceDisabled):
        await resolve_org_connection(db_session, missing)


def test_parse_helpers_shapes() -> None:
    assert parse_service_ref("global/redash") == ("global", "redash")
    assert parse_service_ref("Global/Redash") == ("global", "Redash")
    assert parse_tool_id("global/redash/query") == ("global", "redash", "query")
    for bad in ("", "redash", "global", "/x", "global/", "a/b/c"):
        with pytest.raises(ServiceNotFound):
            parse_service_ref(bad)
    for bad in ("", "global/x", "global//q", "g/s/n/extra"):
        with pytest.raises(ServiceNotFound):
            parse_tool_id(bad)
