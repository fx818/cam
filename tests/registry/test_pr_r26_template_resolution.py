"""R26 — template resolution is owner-qualified/stable and rows carry handles.

Templates are addressed `global/<slug>`; resolution previously preferred
own-then-arbitrary-shared silently. Now: `<handle>/<slug>` resolves to a
template the named owner funds (their own connection, or a share), the shared
fallback is deterministic, and `list_services(include_templates=True)` reports
the handle of the connection owner whose credential will be used.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cam.access import ServiceNotFound, list_services, resolve_service
from cam.access.shares import add_share
from cam.accounts.users import User, create_user
from cam.connections.orgs import ensure_org
from cam.connections.tokens import AuthenticatedIdentity, create_token
from cam.db.models import Connection, Org, Service
from cam.registry.services import register_service


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _user(db_session: AsyncSession, org: Org) -> User:
    return await create_user(
        db_session,
        org_id=org.id,
        email=f"r26-{uuid.uuid4().hex[:10]}@example.com",
        password="pw-whatever",
        role="user",
    )


async def _identity(db_session: AsyncSession, user: User) -> AuthenticatedIdentity:
    _raw, token = await create_token(db_session, org_id=user.org_id, user_id=user.id, label="r26")
    return AuthenticatedIdentity(org_id=user.org_id, user=user, token=token)


async def _template_with_share(
    db_session: AsyncSession,
) -> tuple[Service, User, User, User]:
    org = await ensure_org(db_session)
    alice = await _user(db_session, org)
    bob = await _user(db_session, org)
    stranger = await _user(db_session, org)
    template, _conn = await register_service(
        db_session,
        tier="template",
        slug=_unique("notion"),
        name="Notion",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="none",
        org_id=org.id,
    )
    connection = Connection(
        org_id=org.id,
        service_id=template.id,
        user_id=alice.id,
        status="connected",
    )
    db_session.add(connection)
    await db_session.flush()
    await add_share(db_session, connection_id=connection.id, grantee_user_id=bob.id, actor=alice)
    await db_session.flush()
    return template, alice, bob, stranger


async def test_template_rows_carry_connection_owner_handle(db_session: AsyncSession) -> None:
    template, alice, bob, stranger = await _template_with_share(db_session)
    ref = f"global/{template.slug}"

    id_alice = await _identity(db_session, alice)
    id_bob = await _identity(db_session, bob)
    id_stranger = await _identity(db_session, stranger)

    rows_alice = await list_services(db_session, id_alice, include_templates=True)
    row_alice = next(row for row in rows_alice if row.id == ref)
    assert row_alice.owner == alice.handle
    assert row_alice.shared_by is None

    rows_bob = await list_services(db_session, id_bob, include_templates=True)
    row_bob = next(row for row in rows_bob if row.id == ref)
    assert row_bob.owner == alice.handle
    assert row_bob.shared_by == alice.handle

    rows_stranger = await list_services(db_session, id_stranger, include_templates=True)
    row_stranger = next(row for row in rows_stranger if row.id == ref)
    assert row_stranger.owner is None
    assert row_stranger.shared_by is None


async def test_owner_qualified_template_resolution(db_session: AsyncSession) -> None:
    template, alice, bob, stranger = await _template_with_share(db_session)
    canonical = f"global/{template.slug}"
    owner_qualified = f"{alice.handle}/{template.slug}"

    id_alice = await _identity(db_session, alice)
    id_bob = await _identity(db_session, bob)
    id_stranger = await _identity(db_session, stranger)

    # The connection owner can address the template by their own handle; the
    # canonical ref is reported back.
    resolved_alice = await resolve_service(db_session, id_alice, owner_qualified)
    assert resolved_alice.service.id == template.id
    assert resolved_alice.ref == canonical

    # A user holding a share from alice can use the owner-qualified ref too.
    resolved_bob = await resolve_service(db_session, id_bob, owner_qualified)
    assert resolved_bob.service.id == template.id

    # A user with neither ownership nor a share cannot.
    with pytest.raises(ServiceNotFound):
        await resolve_service(db_session, id_stranger, owner_qualified)

    # The canonical global ref is still reachable for everyone with visibility.
    resolved_global = await resolve_service(db_session, id_stranger, canonical)
    assert resolved_global.service.id == template.id
