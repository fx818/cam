import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.ext.asyncio import (
    async_sessionmaker as SessionMaker,
)

from cam.access.credentials import CredentialExpiredError
from cam.connections.orgs import ensure_org
from cam.db.models import Connection, Service
from cam.db.session import create_session_factory
from cam.jobs import health as health_module
from cam.jobs.health import health_for_invoke, record_invoke_health, run_health_checks
from cam.registry.services import register_global_mcp
from fixtures.mock_mcp import MockMCPHandle

TEST_DATABASE_URL = os.environ.get(
    "CAM_TEST_DATABASE_URL", "postgresql+asyncpg://cam:cam@localhost:5432/cam_test"
)

Maker = SessionMaker[AsyncSession]


@pytest.fixture
async def maker() -> AsyncIterator[Maker]:
    engine = create_async_engine(TEST_DATABASE_URL)
    session_maker = create_session_factory(engine)
    yield session_maker
    await engine.dispose()


async def test_health_for_invoke_mapping() -> None:
    assert health_for_invoke(True, None) == "healthy"
    assert health_for_invoke(False, "UPSTREAM_AUTH") == "failing"
    assert health_for_invoke(False, "TIMEOUT") == "failing"
    assert health_for_invoke(False, "CONNECTION_EXPIRED") == "needs_reconnect"
    assert health_for_invoke(False, "CAM_RATE_LIMITED") is None
    assert health_for_invoke(False, "INTERNAL") is None


async def _register(session: AsyncSession, suffix: str, url: str) -> tuple[Service, Connection]:
    service, connection = await register_global_mcp(
        session,
        slug=f"hc-{suffix}",
        name="Health mock",
        url=url,
        transport="streamable-http",
        auth_type="none",
    )
    await session.flush()
    return service, connection


async def service_health(maker: Maker, service_id: uuid.UUID) -> str:
    async with maker() as db:
        result = await db.execute(select(Service).where(Service.id == service_id))
        row = result.scalar_one()
        return str(row.health)


async def connection_status(maker: Maker, connection_id: uuid.UUID) -> str:
    async with maker() as db:
        result = await db.execute(select(Connection).where(Connection.id == connection_id))
        row = result.scalar_one()
        return str(row.status)


async def test_periodic_probe_marks_healthy_and_failing(
    maker: Maker, mock_mcp: MockMCPHandle
) -> None:
    async with maker() as db:
        good, _ = await _register(db, uuid.uuid4().hex[:8], mock_mcp.streamable_url)
        dead, _ = await _register(db, uuid.uuid4().hex[:8], "http://127.0.0.1:9/mcp")
        good_id, dead_id = good.id, dead.id
        await db.commit()
    probed = await run_health_checks(maker)
    assert probed >= 2
    assert await service_health(maker, good_id) == "healthy"
    assert await service_health(maker, dead_id) == "failing"


async def test_probe_credential_expiry_marks_needs_reconnect(
    maker: Maker, mock_mcp: MockMCPHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def resolve_expired(
        db: AsyncSession, *, service: Service, connection: Connection
    ) -> None:
        raise CredentialExpiredError("refresh failed")

    monkeypatch.setattr(health_module, "resolve_credential", resolve_expired)
    async with maker() as db:
        service, connection = await _register(db, uuid.uuid4().hex[:8], mock_mcp.streamable_url)
        service_id, connection_id = service.id, connection.id
        await db.commit()
    await run_health_checks(maker)
    assert await service_health(maker, service_id) == "needs_reconnect"
    assert await connection_status(maker, connection_id) == "needs_reconnect"


async def test_probe_bearer_mismatch_turns_service_red(
    maker: Maker, mock_mcp_authed: MockMCPHandle
) -> None:
    async with maker() as db:
        service, _connection = await register_global_mcp(
            db,
            slug=f"hcbad-{uuid.uuid4().hex[:8]}",
            name="Auth bad",
            url=mock_mcp_authed.streamable_url,
            transport="streamable-http",
            auth_type="bearer",
            credential="wrong-secret",
        )
        service_id = service.id
        await db.commit()
    await run_health_checks(maker)
    assert await service_health(maker, service_id) == "failing"


async def test_record_invoke_health_roundtrip(maker: Maker, db_session: AsyncSession) -> None:
    org = await ensure_org(db_session)
    service = Service(
        org_id=org.id,
        tier="global",
        slug=f"hcinv-{uuid.uuid4().hex[:8]}",
        name="Inv",
        url="https://example.com/mcp",
        health="healthy",
    )
    db_session.add(service)
    await db_session.flush()
    connection = Connection(org_id=org.id, service_id=service.id, user_id=None, key_version=1)
    db_session.add(connection)
    await db_session.commit()
    service_id, connection_id = service.id, connection.id

    await record_invoke_health(
        maker,
        service_id=service_id,
        connection_id=connection_id,
        ok=False,
        error_code="CONNECTION_EXPIRED",
    )
    assert await service_health(maker, service_id) == "needs_reconnect"

    await record_invoke_health(
        maker,
        service_id=service_id,
        connection_id=connection_id,
        ok=True,
        error_code=None,
    )
    assert await service_health(maker, service_id) == "healthy"
