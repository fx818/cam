"""Deactivation: revoke tokens, disable connections, close sessions, gate access."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.access import (
    OwnerDeactivated,
    ServiceNotFound,
    active_share,
    add_share,
    list_services,
    list_tools,
    resolve_service,
    resolve_tool,
)
from cam.accounts.users import (
    EmailAlreadyRegistered,
    HandleAlreadyTaken,
    UserError,
    create_user,
    deactivate_user,
)
from cam.audit.writer import AuditWriter
from cam.config import Settings
from cam.connections.orgs import ensure_org
from cam.connections.tokens import (
    AuthenticatedIdentity,
    authenticate_token,
    create_token,
)
from cam.db.models import AuditLog, Connection, Service, Tool, User
from cam.db.session import create_db_engine, create_session_factory
from cam.invoke.errors import CamErrorCode, owner_deactivated
from cam.invoke.pipeline import AgentInfo, GatewayDeps, handle
from cam.invoke.rate_limit import RateLimiter
from cam.registry.services import register_personal_mcp
from cam.upstream.pool import SessionPool
from cam.upstream.types import ConnectionTarget
from fixtures.mock_mcp import MockMCPHandle

OWNER_MESSAGE = "This service is no longer available: its owner's account is deactivated."


async def _make_identity(
    db_session: AsyncSession, suffix: str
) -> tuple[AuthenticatedIdentity, str]:
    org = await ensure_org(db_session)
    user = User(
        org_id=org.id,
        email=f"deact-{suffix}@example.com",
        handle=f"deact{suffix}",
        display_name="Deact Test",
        password_hash="x",
        role="user",
        kind="person",
    )
    db_session.add(user)
    await db_session.flush()
    raw, token = await create_token(db_session, org_id=org.id, user_id=user.id, label="deact")
    return AuthenticatedIdentity(org_id=org.id, user=user, token=token), raw


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _personal_service(
    db_session: AsyncSession, owner: User, *, url: str = "https://owned.example.com/mcp"
) -> tuple[Service, Connection]:
    return await register_personal_mcp(
        db_session,
        owner=owner,
        slug=_unique("owned"),
        name="Owned MCP",
        url=url,
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


async def test_deactivate_revokes_tokens_and_disables_connections(
    db_session: AsyncSession,
) -> None:
    identity, raw_token = await _make_identity(db_session, uuid.uuid4().hex[:8] + "a")
    _service, connection = await _personal_service(db_session, identity.user)
    assert await authenticate_token(db_session, raw_token) is not None
    assert connection.status == "connected"

    deactivated = await deactivate_user(db_session, user=identity.user)
    assert deactivated.status == "deactivated"
    assert deactivated.deactivated_at is not None
    assert await authenticate_token(db_session, raw_token) is None
    refreshed = await db_session.get(Connection, connection.id)
    assert refreshed is not None
    assert refreshed.status == "disabled"

    again = await deactivate_user(db_session, user=identity.user)
    assert again.deactivated_at == deactivated.deactivated_at
    still_gone = await db_session.get(User, identity.user.id)
    assert still_gone is not None
    assert still_gone.status == "deactivated"


async def test_deactivate_argument_errors(db_session: AsyncSession) -> None:
    identity, _raw = await _make_identity(db_session, uuid.uuid4().hex[:8] + "b")
    with pytest.raises(UserError, match="exactly one"):
        await deactivate_user(db_session)
    with pytest.raises(UserError, match="exactly one"):
        await deactivate_user(db_session, email=identity.user.email, user=identity.user)
    with pytest.raises(UserError, match="no user with email"):
        await deactivate_user(db_session, email=f"ghost-{uuid.uuid4().hex[:8]}@example.com")


async def test_handle_stays_reserved_after_deactivation(db_session: AsyncSession) -> None:
    identity, _raw = await _make_identity(db_session, uuid.uuid4().hex[:8] + "c")
    await deactivate_user(db_session, user=identity.user)
    with pytest.raises(EmailAlreadyRegistered):
        await create_user(
            db_session,
            email=identity.user.email,
            password="pw-new-12345",
            role="user",
            kind="person",
        )
    with pytest.raises(HandleAlreadyTaken):
        await create_user(
            db_session,
            email=f"{identity.user.handle}-other@example.com",
            password="pw-new-12345",
            role="user",
            kind="person",
            handle=identity.user.handle,
        )


async def test_deactivate_closes_pooled_sessions(
    db_session: AsyncSession, mock_mcp: MockMCPHandle
) -> None:
    identity, _raw = await _make_identity(db_session, uuid.uuid4().hex[:8] + "d")
    service, connection = await _personal_service(
        db_session, identity.user, url=mock_mcp.streamable_url
    )
    pool = SessionPool(idle_timeout_s=60.0, call_timeout_s=10.0)
    try:
        target = ConnectionTarget(
            service_id=str(service.id),
            connection_id=str(connection.id),
            url=mock_mcp.streamable_url,
            transport="streamable-http",
        )
        session = await pool.session(target)
        assert not session.closed
        await pool.invalidate_connection(str(uuid.uuid4()), str(uuid.uuid4()))
        await deactivate_user(db_session, user=identity.user, pool=pool)
        assert session.closed
    finally:
        await pool.aclose()


async def test_owner_deactivated_for_share_holders_only(db_session: AsyncSession) -> None:
    owner, _owner_raw = await _make_identity(db_session, uuid.uuid4().hex[:8] + "e")
    grantee, _grantee_raw = await _make_identity(db_session, uuid.uuid4().hex[:8] + "f")
    stranger, _stranger_raw = await _make_identity(db_session, uuid.uuid4().hex[:8] + "g")
    service, connection = await _personal_service(db_session, owner.user)
    await _add_tool(db_session, owner.org_id, service, "send")
    ref = f"{owner.user.handle}/{service.slug}"
    tool_id = f"{ref}/send"

    share = await add_share(
        db_session,
        connection_id=connection.id,
        grantee_user_id=grantee.user.id,
        actor=owner.user,
    )
    resolved = await resolve_service(db_session, grantee, ref)
    assert resolved.service.id == service.id

    await deactivate_user(db_session, user=owner.user)

    with pytest.raises(OwnerDeactivated) as exc_info:
        await resolve_service(db_session, grantee, ref)
    audit_ref = exc_info.value.audit_ref
    assert audit_ref is not None
    assert audit_ref.service_id == service.id
    with pytest.raises(OwnerDeactivated):
        await resolve_tool(db_session, grantee, tool_id)
    with pytest.raises(OwnerDeactivated):
        await list_tools(db_session, grantee, ref)
    grantee_ids = {summary.id for summary in await list_services(db_session, grantee)}
    assert ref not in grantee_ids

    with pytest.raises(ServiceNotFound):
        await resolve_service(db_session, stranger, ref)
    with pytest.raises(ServiceNotFound):
        await resolve_tool(db_session, stranger, tool_id)
    stranger_ids = {summary.id for summary in await list_services(db_session, stranger)}
    assert ref not in stranger_ids

    still_shared = await active_share(
        db_session, connection_id=connection.id, grantee_user_id=grantee.user.id
    )
    assert still_shared is not None
    assert still_shared.id == share.id

    owner_row = await db_session.get(User, owner.user.id)
    assert owner_row is not None
    assert owner_row.status == "deactivated"
    service_row = await db_session.get(Service, service.id)
    assert service_row is not None
    assert service_row.status == "active"


def test_owner_deactivated_error_envelope() -> None:
    error = owner_deactivated()
    assert error.code is CamErrorCode.OWNER_DEACTIVATED
    assert error.message == OWNER_MESSAGE
    assert error.retryable is False
    envelope = error.envelope()
    assert envelope["error"]["code"] == "OWNER_DEACTIVATED"
    assert envelope["error"]["message"] == OWNER_MESSAGE


async def test_pipeline_maps_owner_deactivated_to_envelope(db_session: AsyncSession) -> None:
    owner, _owner_raw = await _make_identity(db_session, uuid.uuid4().hex[:8] + "h")
    grantee, _grantee_raw = await _make_identity(db_session, uuid.uuid4().hex[:8] + "i")
    service, connection = await _personal_service(db_session, owner.user)
    await _add_tool(db_session, owner.org_id, service, "send")
    ref = f"{owner.user.handle}/{service.slug}"
    tool_id = f"{ref}/send"
    await add_share(
        db_session,
        connection_id=connection.id,
        grantee_user_id=grantee.user.id,
        actor=owner.user,
    )
    await deactivate_user(db_session, user=owner.user)
    await db_session.commit()

    engine = create_db_engine(Settings().database_url)
    factory = create_session_factory(engine)
    pool = SessionPool(idle_timeout_s=60.0, call_timeout_s=10.0)
    audit = AuditWriter(factory)
    deps = GatewayDeps(
        session_factory=factory,
        pool=pool,
        settings=Settings(),
        rate_limiter=RateLimiter(60),
        audit=audit,
    )
    try:
        result = await handle(
            deps,
            grantee,
            AgentInfo(name="phase2-tests", version="0"),
            action="describe",
            id=tool_id,
        )
        assert result.is_error is True
        assert result.content
        payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
        error = payload["error"]
        assert error["code"] == "OWNER_DEACTIVATED"
        assert error["message"] == OWNER_MESSAGE
        assert error["service"] == ref
        assert error["retryable"] is False

        await audit.flush()
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditLog).where(AuditLog.error_code == "OWNER_DEACTIVATED")
                    )
                )
                .scalars()
                .all()
            )
            match = next(row for row in rows if row.service_id == service.id)
            assert match.user_id == grantee.user.id
            assert match.error_message == OWNER_MESSAGE
    finally:
        await audit.aclose()
        await pool.aclose()
        await engine.dispose()
