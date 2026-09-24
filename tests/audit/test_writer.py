"""Unit tests for the async audit-log writer."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from cam.audit.writer import AuditEvent, AuditWriter
from cam.config import Settings
from cam.connections.orgs import ensure_org
from cam.db.models import AuditLog
from cam.db.session import create_session_factory


@pytest.fixture
async def org_id(db_session: AsyncSession) -> uuid.UUID:
    org = await ensure_org(db_session)
    await db_session.commit()
    return org.id


def _event(org_id: uuid.UUID, *, action: str = "invoke", marker: str | None = "x") -> AuditEvent:
    return AuditEvent(
        org_id=org_id,
        action=action,
        args={"text": marker} if action == "invoke" and marker is not None else None,
        agent_name="claude-code",
        agent_version="1.0.0",
        status="ok",
        latency_ms=12,
        response_bytes=34,
    )


async def _count(engine: AsyncEngine, org_id: uuid.UUID, prefix: str) -> int:
    factory = create_session_factory(engine)
    async with factory() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.org_id == org_id,
                AuditLog.args["text"].astext.startswith(prefix),
            )
        )
        return len(list(result.scalars()))


@pytest.fixture
async def writer_env(org_id: uuid.UUID) -> AsyncIterator[tuple[AuditWriter, AsyncEngine]]:
    engine = create_async_engine(Settings().database_url, pool_pre_ping=True)
    factory = create_session_factory(engine)
    writer = AuditWriter(factory)
    try:
        yield writer, engine
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_record_and_flush_persists_rows(
    writer_env: tuple[AuditWriter, AsyncEngine], org_id: uuid.UUID
) -> None:
    writer, engine = writer_env
    prefix = f"w-flush-{uuid.uuid4().hex}-"
    await writer.start()
    assert writer.record(_event(org_id, marker=f"{prefix}1")) is True
    assert writer.record(_event(org_id, action="list_services", marker=None)) is True
    await writer.flush()
    assert await _count(engine, org_id, prefix) == 1
    factory = create_session_factory(engine)
    async with factory() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.org_id == org_id,
                AuditLog.action == "list_services",
                AuditLog.agent_name == "claude-code",
                AuditLog.latency_ms == 12,
            )
        )
        rows = [r for r in result.scalars() if r.args is None]
        assert len(rows) >= 1


async def test_flush_autostarts_when_never_started(
    writer_env: tuple[AuditWriter, AsyncEngine], org_id: uuid.UUID
) -> None:
    writer, engine = writer_env
    prefix = f"w-auto-{uuid.uuid4().hex}-"
    assert writer.record(_event(org_id, marker=f"{prefix}1")) is True
    await writer.flush()
    assert await _count(engine, org_id, prefix) == 1


async def test_queue_full_drops_event(org_id: uuid.UUID) -> None:
    engine = create_async_engine(Settings().database_url, pool_pre_ping=True)
    factory = create_session_factory(engine)
    writer = AuditWriter(factory, max_queue=1)
    keep = f"w-q-keep-{uuid.uuid4().hex}-"
    drop = f"w-q-drop-{uuid.uuid4().hex}-"
    try:
        assert writer.record(_event(org_id, marker=f"{keep}1")) is True
        assert writer.record(_event(org_id, marker=f"{drop}1")) is False
        await writer.flush()
        assert await _count(engine, org_id, keep) == 1
        assert await _count(engine, org_id, drop) == 0
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_aclose_drains_pending_without_prior_start(org_id: uuid.UUID) -> None:
    engine = create_async_engine(Settings().database_url, pool_pre_ping=True)
    factory = create_session_factory(engine)
    writer = AuditWriter(factory)
    prefix = f"w-close-{uuid.uuid4().hex}-"
    assert writer.record(_event(org_id, marker=f"{prefix}1")) is True
    await writer.aclose()
    assert await _count(engine, org_id, prefix) == 1
    assert writer.record(_event(org_id, marker=f"{prefix}2")) is False
    await engine.dispose()


async def test_write_failure_does_not_raise(org_id: uuid.UUID) -> None:
    engine = create_async_engine(
        "postgresql+asyncpg://cam:cam@127.0.0.1:1/unreachable",
        connect_args={"timeout": 1.0, "command_timeout": 1.0},
    )
    factory = create_session_factory(engine)
    writer = AuditWriter(factory)
    await writer.start()
    assert writer.record(_event(org_id, marker="w-dead-1")) is True
    await writer.flush()
    await writer.aclose()
    await engine.dispose()


async def test_batches_larger_than_batch_size(
    writer_env: tuple[AuditWriter, AsyncEngine], org_id: uuid.UUID
) -> None:
    writer, engine = writer_env
    prefix = f"w-batch-{uuid.uuid4().hex}-"
    await writer.start()
    for i in range(60):
        assert writer.record(_event(org_id, marker=f"{prefix}{i}")) is True
    await writer.flush()
    assert await _count(engine, org_id, prefix) == 60
