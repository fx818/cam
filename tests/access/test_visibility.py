"""Global visibility: admin grants and the tier-aware access filter."""

from __future__ import annotations

import uuid
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cam.access import ServiceNotFound, list_services, resolve_service, resolve_tool
from cam.access.resolution import has_access
from cam.connections.orgs import ensure_org
from cam.connections.tokens import AuthenticatedIdentity, create_token
from cam.db.models import Service, ServiceVisibility, Tool, User
from cam.registry.services import (
    ServiceError,
    ServiceVisibilityLevel,
    grant_service_visibility,
    register_global_mcp,
    revoke_service_visibility,
    set_service_visibility,
)


async def _make_identity(db_session: AsyncSession, suffix: str) -> AuthenticatedIdentity:
    org = await ensure_org(db_session)
    user = User(
        org_id=org.id,
        email=f"vis-{suffix}@example.com",
        handle=f"vis{suffix}",
        display_name="Visibility Test",
        password_hash="x",
        role="user",
        kind="person",
    )
    db_session.add(user)
    await db_session.flush()
    _raw, token = await create_token(db_session, org_id=org.id, user_id=user.id, label="vis")
    return AuthenticatedIdentity(org_id=org.id, user=user, token=token)


async def _make_global_service(db_session: AsyncSession, slug: str) -> Service:
    service, _connection = await register_global_mcp(
        db_session,
        slug=slug,
        name=slug,
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="none",
    )
    return service


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def test_set_grant_revoke_visibility_flow(db_session: AsyncSession) -> None:
    identity_a = await _make_identity(db_session, uuid.uuid4().hex[:8] + "a")
    identity_b = await _make_identity(db_session, uuid.uuid4().hex[:8] + "b")
    service = await _make_global_service(db_session, _unique("vault"))
    ref = f"global/{service.slug}"
    tool = Tool(
        org_id=service.org_id,
        service_id=service.id,
        name="query",
        upstream_name="query",
        description="q",
        input_schema={"type": "object", "properties": {}},
    )
    db_session.add(tool)
    await db_session.flush()

    service = await set_service_visibility(db_session, slug=service.slug, visibility="restricted")
    assert service.visibility == "restricted"
    with pytest.raises(ServiceNotFound):
        await resolve_service(db_session, identity_a, ref)
    with pytest.raises(ServiceNotFound):
        await resolve_tool(db_session, identity_a, f"{ref}/query")
    listed_a = {s.id for s in await list_services(db_session, identity_a)}
    assert ref not in listed_a

    service = await grant_service_visibility(
        db_session, slug=service.slug, user_email=identity_a.user.email
    )
    assert service.visibility == "restricted"
    resolved = await resolve_service(db_session, identity_a, ref)
    assert resolved.service.id == service.id
    listed_a = {s.id for s in await list_services(db_session, identity_a)}
    assert ref in listed_a
    listed_b = {s.id for s in await list_services(db_session, identity_b)}
    assert ref not in listed_b

    service = await revoke_service_visibility(
        db_session, slug=service.slug, user_email=identity_a.user.email
    )
    assert service.visibility == "restricted"
    with pytest.raises(ServiceNotFound):
        await resolve_service(db_session, identity_a, ref)
    listed_a = {s.id for s in await list_services(db_session, identity_a)}
    assert ref not in listed_a

    service = await set_service_visibility(db_session, slug=service.slug, visibility="all")
    resolved = await resolve_service(db_session, identity_b, ref)
    assert resolved.service.id == service.id


async def test_visibility_admin_error_cases(db_session: AsyncSession) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "e")
    service = await _make_global_service(db_session, _unique("errsvc"))
    with pytest.raises(ServiceError, match="visibility"):
        await set_service_visibility(
            db_session, slug=service.slug, visibility=cast(ServiceVisibilityLevel, "maybe")
        )
    with pytest.raises(ServiceError, match="no global or template"):
        await set_service_visibility(db_session, slug=_unique("ghost"), visibility="restricted")
    with pytest.raises(ServiceError, match="no user with email"):
        await grant_service_visibility(
            db_session, slug=service.slug, user_email="ghost@example.com"
        )
    await grant_service_visibility(db_session, slug=service.slug, user_email=identity.user.email)
    with pytest.raises(ServiceError, match="already has visibility"):
        await grant_service_visibility(
            db_session, slug=service.slug, user_email=identity.user.email
        )
    await revoke_service_visibility(db_session, slug=service.slug, user_email=identity.user.email)
    with pytest.raises(ServiceError, match="no visibility grant"):
        await revoke_service_visibility(
            db_session, slug=service.slug, user_email=identity.user.email
        )


async def test_grant_rejects_user_from_another_org(db_session: AsyncSession) -> None:
    service = await _make_global_service(db_session, _unique("crossvis"))
    other_org = await ensure_org(db_session, slug=f"other-{uuid.uuid4().hex[:8]}", name="Other Org")
    outsider = User(
        org_id=other_org.id,
        email=f"outsider-{uuid.uuid4().hex[:8]}@example.com",
        handle=f"out{uuid.uuid4().hex[:8]}",
        display_name="Outsider",
        password_hash="x",
        role="user",
        kind="person",
    )
    db_session.add(outsider)
    await db_session.flush()
    with pytest.raises(ServiceError, match="different org"):
        await grant_service_visibility(db_session, slug=service.slug, user_email=outsider.email)


async def test_visibility_rows_do_not_grant_personal_access(db_session: AsyncSession) -> None:
    identity_a = await _make_identity(db_session, uuid.uuid4().hex[:8] + "p")
    identity_b = await _make_identity(db_session, uuid.uuid4().hex[:8] + "q")
    personal = Service(
        org_id=identity_a.org_id,
        tier="personal",
        owner_user_id=identity_a.user.id,
        slug=_unique("mine"),
        name="Mine",
        url="https://example.com/mcp",
        transport="streamable-http",
        visibility="all",
    )
    db_session.add(personal)
    await db_session.flush()
    db_session.add(
        ServiceVisibility(
            org_id=identity_a.org_id,
            service_id=personal.id,
            user_id=identity_b.user.id,
        )
    )
    await db_session.flush()

    assert await has_access(db_session, identity_a, personal) is True
    assert await has_access(db_session, identity_b, personal) is False
