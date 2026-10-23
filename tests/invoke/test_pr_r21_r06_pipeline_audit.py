"""R21: invoke audit carries the credential owner.
R06 (pipeline side): a rejected audit event is surfaced, not silently dropped.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cam.audit.writer import AuditWriter
from cam.config import Settings
from cam.connections.orgs import ensure_org
from cam.connections.tokens import AuthenticatedIdentity, create_token
from cam.db.models import AuditLog, Connection, Service, Tool, User
from cam.db.session import create_db_engine, create_session_factory
from cam.invoke import pipeline
from cam.invoke.pipeline import AgentInfo, GatewayDeps, handle
from cam.invoke.rate_limit import RateLimiter
from cam.upstream.pool import SessionPool
from cam.upstream.types import TextBlock, ToolCallOutcome


@pytest.fixture(autouse=True)
def _enable_pipeline_logger() -> None:
    # The session-scoped migration fixture runs alembic, whose logging config
    # disables loggers created at import time; re-enable this one so caplog can
    # observe the warnings the pipeline emits.
    logging.getLogger("cam.invoke.pipeline").disabled = False


async def _invoke_env(
    db_session: AsyncSession,
) -> tuple[AuthenticatedIdentity, Service, uuid.UUID, str]:
    org = await ensure_org(db_session)
    handle_name = f"pr{uuid.uuid4().hex[:8]}"
    user = User(
        org_id=org.id,
        email=f"{handle_name}@example.com",
        handle=handle_name,
        display_name="PR User",
        password_hash="x",
        role="user",
        kind="person",
    )
    db_session.add(user)
    await db_session.flush()
    _raw, token = await create_token(db_session, org_id=org.id, user_id=user.id, label="pr")
    service = Service(
        org_id=org.id,
        tier="personal",
        owner_user_id=user.id,
        slug=f"svc-{uuid.uuid4().hex[:8]}",
        name="PR REST",
        kind="rest",
        url="https://example.com/api",
        transport=None,
        auth_type="none",
        auth_config={},
        visibility="all",
        status="active",
        health="unknown",
    )
    db_session.add(service)
    await db_session.flush()
    connection = Connection(org_id=org.id, service_id=service.id, user_id=user.id, key_version=1)
    db_session.add(connection)
    await db_session.flush()
    tool = Tool(
        org_id=org.id,
        service_id=service.id,
        name="run",
        upstream_name="run",
        description="run it",
        input_schema={"type": "object", "properties": {}},
        enabled=True,
        status="active",
    )
    db_session.add(tool)
    await db_session.flush()
    await db_session.commit()
    identity = AuthenticatedIdentity(org_id=org.id, user=user, token=token)
    return identity, service, user.id, f"{handle_name}/{service.slug}/run"


def _deps(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[GatewayDeps, SessionPool, AuditWriter]:
    pool = SessionPool(idle_timeout_s=60.0, call_timeout_s=10.0)
    audit = AuditWriter(session_factory)
    deps = GatewayDeps(
        session_factory=session_factory,
        pool=pool,
        settings=Settings(),
        rate_limiter=RateLimiter(60),
        audit=audit,
    )
    return deps, pool, audit


def _ok_outcome() -> ToolCallOutcome:
    return ToolCallOutcome(content=(TextBlock(text="done"),), structured=None, is_error=False)


async def test_invoke_audit_records_the_credential_owner(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, service, owner_id, tool_id = await _invoke_env(db_session)
    engine = create_db_engine(Settings().database_url)
    factory = create_session_factory(engine)
    deps, pool, audit = _deps(factory)

    async def _fake_execute_rest(**kwargs: Any) -> ToolCallOutcome:
        return _ok_outcome()

    monkeypatch.setattr(pipeline, "execute_rest", _fake_execute_rest)
    try:
        result = await handle(
            deps,
            identity,
            AgentInfo(name="pr", version="0"),
            action="invoke",
            id=tool_id,
            args={},
        )
        assert result.is_error is False
        await audit.flush()
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditLog).where(
                            AuditLog.service_id == service.id,
                            AuditLog.action == "invoke",
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await audit.aclose()
        await pool.aclose()
        await engine.dispose()

    assert rows
    assert all(row.owner_user_id == owner_id for row in rows)


async def test_rejected_audit_event_is_surfaced_not_fatal(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    identity, _service, _owner_id, tool_id = await _invoke_env(db_session)
    engine = create_db_engine(Settings().database_url)
    factory = create_session_factory(engine)
    deps, pool, audit = _deps(factory)

    async def _fake_execute_rest(**kwargs: Any) -> ToolCallOutcome:
        return _ok_outcome()

    monkeypatch.setattr(pipeline, "execute_rest", _fake_execute_rest)
    # Closing the writer makes `record()` return False for every event.
    await audit.aclose()
    try:
        with caplog.at_level(logging.WARNING, logger="cam.invoke.pipeline"):
            result = await handle(
                deps,
                identity,
                AgentInfo(name="pr", version="0"),
                action="invoke",
                id=tool_id,
                args={},
            )
    finally:
        await audit.aclose()
        await pool.aclose()
        await engine.dispose()

    assert result.is_error is False
    assert result.content
    assert any("audit event not recorded" in record.message for record in caplog.records)
