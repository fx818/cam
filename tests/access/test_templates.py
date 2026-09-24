"""Template services: listed for everyone, invoked with a per-user connection."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cam.access import (
    ServiceDisabled,
    ServiceNotFound,
    add_share,
    list_services,
    resolve_connection,
    resolve_service,
    service_ref,
)
from cam.connections.orgs import ensure_org
from cam.connections.tokens import AuthenticatedIdentity, create_token
from cam.db.models import Connection, User
from cam.registry.services import register_service


async def _make_user(db_session: AsyncSession, suffix: str) -> User:
    org = await ensure_org(db_session)
    user = User(
        org_id=org.id,
        email=f"tmpl-{suffix}@example.com",
        handle=f"tmpl{suffix}",
        display_name=f"Template {suffix}",
        password_hash="x",
        role="user",
        kind="person",
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def _identity(db_session: AsyncSession, user: User) -> AuthenticatedIdentity:
    _raw, token = await create_token(db_session, org_id=user.org_id, user_id=user.id, label="tmpl")
    return AuthenticatedIdentity(org_id=user.org_id, user=user, token=token)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _register_template(
    db_session: AsyncSession, *, slug: str, auth_type: str = "bearer"
) -> Any:
    service, _conn = await register_service(
        db_session,
        tier="template",
        slug=slug,
        name="Shared Template",
        url="https://api.example.com/v1",
        kind="rest",
        auth_type=cast("Any", auth_type),
        credential="placeholder" if auth_type != "none" else None,
        description="template service",
    )
    return service


async def test_template_reachable_through_global_ref(db_session: AsyncSession) -> None:
    user = await _make_user(db_session, uuid.uuid4().hex[:10])
    slug = _unique("tplsvc")
    service = await _register_template(db_session, slug=slug, auth_type="none")
    identity = await _identity(db_session, user)
    resolved = await resolve_service(db_session, identity, f"global/{slug}")
    assert resolved.service.id == service.id
    assert resolved.ref == f"global/{slug}"
    assert service_ref(service) == f"global/{slug}"


async def test_template_not_found_in_other_org(db_session: AsyncSession) -> None:
    org = await ensure_org(db_session, slug=f"other-{uuid.uuid4().hex[:10]}")
    user = User(
        org_id=org.id,
        email=f"other-tmpl-{uuid.uuid4().hex[:10]}@example.com",
        handle=f"othertmpl{uuid.uuid4().hex[:10]}",
        display_name="Other Org",
        password_hash="x",
        role="user",
        kind="person",
    )
    db_session.add(user)
    await db_session.flush()
    slug = _unique("tplnone")
    _ = await _register_template(db_session, slug=slug)
    identity = await _identity(db_session, user)
    with pytest.raises(ServiceNotFound):
        await resolve_service(db_session, identity, f"global/{slug}")


async def test_resolve_connection_template_own_then_shared_then_disabled(
    db_session: AsyncSession,
) -> None:
    suffix = uuid.uuid4().hex[:10]
    user_a = await _make_user(db_session, "a" + suffix)
    user_b = await _make_user(db_session, "b" + suffix)
    ref = f"global/{_unique('conntpl')}"
    slug = ref.split("/", 1)[1]
    template = await _register_template(db_session, slug=slug)
    identity_b = await _identity(db_session, user_b)

    with pytest.raises(ServiceDisabled) as exc_info:
        await resolve_connection(db_session, template, ref=ref, user_id=user_a.id)
    assert "CAM dashboard" in str(exc_info.value.message)

    with pytest.raises(ServiceDisabled):
        await resolve_connection(db_session, template, ref=ref, user_id=None)

    conn_a = Connection(
        org_id=user_a.org_id,
        service_id=template.id,
        user_id=user_a.id,
        status="connected",
    )
    db_session.add(conn_a)
    await db_session.flush()
    resolved_a = await resolve_connection(db_session, template, ref=ref, user_id=user_a.id)
    assert resolved_a.id == conn_a.id

    share = await add_share(
        db_session,
        connection_id=conn_a.id,
        grantee_user_id=user_b.id,
        actor=user_a,
    )
    resolved_b = await resolve_connection(db_session, template, ref=ref, user_id=identity_b.user.id)
    assert resolved_b.id == conn_a.id

    conn_b = Connection(
        org_id=user_b.org_id,
        service_id=template.id,
        user_id=user_b.id,
        status="connected",
    )
    db_session.add(conn_b)
    await db_session.flush()
    resolved_own = await resolve_connection(
        db_session, template, ref=ref, user_id=identity_b.user.id
    )
    assert resolved_own.id == conn_b.id
    del share


async def test_resolve_connection_template_needs_reconnect_is_disabled(
    db_session: AsyncSession,
) -> None:
    user = await _make_user(db_session, uuid.uuid4().hex[:10])
    slug = _unique("dropped")
    template = await _register_template(db_session, slug=slug)
    conn = Connection(
        org_id=user.org_id,
        service_id=template.id,
        user_id=user.id,
        status="needs_reconnect",
    )
    db_session.add(conn)
    await db_session.flush()
    with pytest.raises(ServiceDisabled):
        await resolve_connection(db_session, template, ref=f"global/{slug}", user_id=user.id)


async def test_list_services_includes_templates_with_connected_flag(
    db_session: AsyncSession,
) -> None:
    user = await _make_user(db_session, uuid.uuid4().hex[:10])
    identity = await _identity(db_session, user)
    slug = _unique("listtpl")
    template = await _register_template(db_session, slug=slug)
    conn = Connection(
        org_id=user.org_id,
        service_id=template.id,
        user_id=user.id,
        status="connected",
    )
    db_session.add(conn)
    await db_session.flush()

    without = await list_services(db_session, identity)
    assert all(summary.tier != "template" for summary in without)

    summaries = await list_services(db_session, identity, include_templates=True)
    match = next(summary for summary in summaries if summary.id == f"global/{slug}")
    assert match.tier == "template"
    assert match.kind == "rest"
    assert match.service_id == str(template.id)
    assert match.connected is True

    conn.status = "needs_reconnect"
    await db_session.flush()
    summaries = await list_services(db_session, identity, include_templates=True)
    match = next(summary for summary in summaries if summary.id == f"global/{slug}")
    assert match.connected is False
