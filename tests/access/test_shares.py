"""Sharing: add, revoke, and access checks at every step."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cam.access import (
    ServiceNotFound,
    ShareError,
    active_share,
    add_share,
    list_services,
    list_shares,
    list_tools,
    resolve_connection,
    resolve_service,
    resolve_tool,
    revoke_share,
    share_service,
    unshare_service,
)
from cam.connections.orgs import ensure_org
from cam.connections.tokens import AuthenticatedIdentity, create_token
from cam.db.models import Connection, Service, Tool, User
from cam.registry.services import register_global_mcp, register_personal_mcp


async def _make_identity(db_session: AsyncSession, suffix: str) -> AuthenticatedIdentity:
    org = await ensure_org(db_session)
    user = User(
        org_id=org.id,
        email=f"share-{suffix}@example.com",
        handle=f"sh{suffix}",
        display_name="Share Test",
        password_hash="x",
        role="user",
        kind="person",
    )
    db_session.add(user)
    await db_session.flush()
    _raw, token = await create_token(db_session, org_id=org.id, user_id=user.id, label="share")
    return AuthenticatedIdentity(org_id=org.id, user=user, token=token)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _personal_service(
    db_session: AsyncSession, owner: User, *, slug: str | None = None
) -> tuple[Service, Connection]:
    return await register_personal_mcp(
        db_session,
        owner=owner,
        slug=slug or _unique("svc"),
        name="Shared MCP",
        url="https://shared.example.com/mcp",
        transport="streamable-http",
        auth_type="none",
    )


async def _add_tool(
    db_session: AsyncSession, org_id: uuid.UUID, service: Service, name: str
) -> Tool:
    tool = Tool(
        org_id=org_id,
        service_id=service.id,
        name=name,
        upstream_name=name,
        description=f"tool {name}",
        input_schema={"type": "object", "properties": {}},
    )
    db_session.add(tool)
    await db_session.flush()
    return tool


async def test_share_grants_access_and_revoke_removes_it_immediately(
    db_session: AsyncSession,
) -> None:
    owner = await _make_identity(db_session, uuid.uuid4().hex[:8] + "a")
    grantee = await _make_identity(db_session, uuid.uuid4().hex[:8] + "b")
    stranger = await _make_identity(db_session, uuid.uuid4().hex[:8] + "c")
    service, connection = await _personal_service(db_session, owner.user)
    await _add_tool(db_session, owner.org_id, service, "send")
    ref = f"{owner.user.handle}/{service.slug}"
    tool_id = f"{ref}/send"

    with pytest.raises(ServiceNotFound):
        await resolve_service(db_session, grantee, ref)

    share = await add_share(
        db_session,
        connection_id=connection.id,
        grantee_user_id=grantee.user.id,
        actor=owner.user,
    )
    assert share.revoked_at is None

    resolved = await resolve_service(db_session, grantee, ref)
    assert resolved.service.id == service.id
    resolved_tool = await resolve_tool(db_session, grantee, tool_id)
    assert resolved_tool.id == tool_id
    resolved_service, tools = await list_tools(db_session, grantee, ref)
    assert resolved_service.ref == ref
    assert [tool.name for tool in tools] == ["send"]
    grantee_connection = await resolve_connection(db_session, service, ref=ref)
    assert grantee_connection.id == connection.id

    summaries = await list_services(db_session, grantee)
    summary = next(s for s in summaries if s.id == ref)
    assert summary.owner == owner.user.handle
    assert summary.shared_by == owner.user.handle
    owner_summaries = await list_services(db_session, owner)
    owner_summary = next(s for s in owner_summaries if s.id == ref)
    assert owner_summary.owner == owner.user.handle
    assert owner_summary.shared_by is None

    assert ref not in {s.id for s in await list_services(db_session, stranger)}
    with pytest.raises(ServiceNotFound):
        await resolve_service(db_session, stranger, ref)

    revoked = await revoke_share(db_session, share_id=share.id, actor=owner.user)
    assert revoked.revoked_at is not None
    with pytest.raises(ServiceNotFound):
        await resolve_service(db_session, grantee, ref)
    with pytest.raises(ServiceNotFound):
        await resolve_tool(db_session, grantee, tool_id)
    with pytest.raises(ServiceNotFound):
        await list_tools(db_session, grantee, ref)
    assert ref not in {s.id for s in await list_services(db_session, grantee)}
    resolved_owner = await resolve_service(db_session, owner, ref)
    assert resolved_owner.service.id == service.id

    reshared = await add_share(
        db_session,
        connection_id=connection.id,
        grantee_user_id=grantee.user.id,
        actor=owner.user,
    )
    assert reshared.id != share.id
    assert reshared.revoked_at is None
    await resolve_service(db_session, grantee, ref)


async def test_share_management_is_owner_only(db_session: AsyncSession) -> None:
    owner = await _make_identity(db_session, uuid.uuid4().hex[:8] + "d")
    grantee = await _make_identity(db_session, uuid.uuid4().hex[:8] + "e")
    intruder = await _make_identity(db_session, uuid.uuid4().hex[:8] + "f")
    _service, connection = await _personal_service(db_session, owner.user)

    with pytest.raises(ShareError, match="connection not found"):
        await add_share(
            db_session,
            connection_id=connection.id,
            grantee_user_id=intruder.user.id,
            actor=intruder.user,
        )
    share = await add_share(
        db_session,
        connection_id=connection.id,
        grantee_user_id=grantee.user.id,
        actor=owner.user,
    )
    with pytest.raises(ShareError, match="connection not found"):
        await revoke_share(db_session, share_id=share.id, actor=intruder.user)
    with pytest.raises(ShareError, match="connection not found"):
        await list_shares(db_session, connection_id=connection.id, actor=intruder.user)
    with pytest.raises(ShareError, match="connection not found"):
        await add_share(
            db_session,
            connection_id=connection.id,
            grantee_user_id=intruder.user.id,
            actor=grantee.user,
        )
    listed = await list_shares(db_session, connection_id=connection.id, actor=owner.user)
    assert [row.id for row in listed] == [share.id]


async def test_share_validation_errors(db_session: AsyncSession) -> None:
    owner = await _make_identity(db_session, uuid.uuid4().hex[:8] + "g")
    grantee = await _make_identity(db_session, uuid.uuid4().hex[:8] + "h")
    _service, connection = await _personal_service(db_session, owner.user)

    with pytest.raises(ShareError, match="yourself"):
        await add_share(
            db_session,
            connection_id=connection.id,
            grantee_user_id=owner.user.id,
            actor=owner.user,
        )

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
    with pytest.raises(ShareError, match="not found in this org"):
        await add_share(
            db_session,
            connection_id=connection.id,
            grantee_user_id=outsider.id,
            actor=owner.user,
        )

    deactivated = await _make_identity(db_session, uuid.uuid4().hex[:8] + "i")
    deactivated.user.status = "deactivated"
    deactivated.user.deactivated_at = datetime.now(UTC)
    await db_session.flush()
    with pytest.raises(ShareError, match="not active"):
        await add_share(
            db_session,
            connection_id=connection.id,
            grantee_user_id=deactivated.user.id,
            actor=owner.user,
        )

    share = await add_share(
        db_session,
        connection_id=connection.id,
        grantee_user_id=grantee.user.id,
        actor=owner.user,
    )
    with pytest.raises(ShareError, match="already shared"):
        await add_share(
            db_session,
            connection_id=connection.id,
            grantee_user_id=grantee.user.id,
            actor=owner.user,
        )
    await revoke_share(db_session, share_id=share.id, actor=owner.user)
    with pytest.raises(ShareError, match="already revoked"):
        await revoke_share(db_session, share_id=share.id, actor=owner.user)
    with pytest.raises(ShareError, match="share not found"):
        await revoke_share(db_session, share_id=uuid.uuid4(), actor=owner.user)
    assert (
        await active_share(db_session, connection_id=connection.id, grantee_user_id=grantee.user.id)
        is None
    )


async def test_global_org_connection_cannot_be_shared(db_session: AsyncSession) -> None:
    owner = await _make_identity(db_session, uuid.uuid4().hex[:8] + "j")
    global_service, org_connection = await register_global_mcp(
        db_session,
        slug=_unique("unshareable"),
        name="Unshareable",
        url="https://unshareable.example.com/mcp",
        transport="streamable-http",
        auth_type="none",
    )
    assert org_connection.user_id is None
    with pytest.raises(ShareError, match="connection not found"):
        await add_share(
            db_session,
            connection_id=org_connection.id,
            grantee_user_id=owner.user.id,
            actor=owner.user,
        )
    with pytest.raises(ShareError, match="visibility"):
        await share_service(
            db_session,
            ref=f"global/{global_service.slug}",
            owner_email=owner.user.email,
            grantee_email=owner.user.email,
        )


async def test_share_service_and_unshare_service_by_ref(db_session: AsyncSession) -> None:
    owner = await _make_identity(db_session, uuid.uuid4().hex[:8] + "k")
    grantee = await _make_identity(db_session, uuid.uuid4().hex[:8] + "l")
    service, _connection = await _personal_service(db_session, owner.user)
    ref = f"{owner.user.handle}/{service.slug}"

    share = await share_service(
        db_session, ref=ref, owner_email=owner.user.email, grantee_email=grantee.user.email
    )
    assert share.revoked_at is None
    await resolve_service(db_session, grantee, ref)

    with pytest.raises(ShareError, match="already shared"):
        await share_service(
            db_session, ref=ref, owner_email=owner.user.email, grantee_email=grantee.user.email
        )
    with pytest.raises(ShareError, match="does not belong"):
        await share_service(
            db_session, ref=ref, owner_email=grantee.user.email, grantee_email=owner.user.email
        )
    with pytest.raises(ShareError, match="no personal service"):
        await share_service(
            db_session,
            ref=f"{owner.user.handle}/ghost",
            owner_email=owner.user.email,
            grantee_email=grantee.user.email,
        )
    with pytest.raises(ShareError, match="no user with email"):
        await share_service(
            db_session,
            ref=ref,
            owner_email=owner.user.email,
            grantee_email="ghost@example.com",
        )
    with pytest.raises(ShareError, match="not shared"):
        await unshare_service(
            db_session, ref=ref, owner_email=owner.user.email, grantee_email=owner.user.email
        )

    revoked = await unshare_service(
        db_session, ref=ref, owner_email=owner.user.email, grantee_email=grantee.user.email
    )
    assert revoked.revoked_at is not None
    with pytest.raises(ServiceNotFound):
        await resolve_service(db_session, grantee, ref)
    with pytest.raises(ShareError, match="not shared"):
        await unshare_service(
            db_session, ref=ref, owner_email=owner.user.email, grantee_email=grantee.user.email
        )
