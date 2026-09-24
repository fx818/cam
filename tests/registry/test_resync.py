import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Literal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cam.db.models import Connection, Service, Tool
from cam.db.session import create_session_factory
from cam.registry.resync import UpstreamToolSpec, apply_diff, resync_by_ref
from cam.registry.services import register_global_mcp, register_personal_mcp
from cam.upstream import ConnectionTarget, UpstreamSession
from fixtures.mock_mcp import MockMCPHandle
from fixtures.mock_rest import MockRestHandle

TEST_DATABASE_URL = "postgresql+asyncpg://cam:cam@localhost:5432/cam_test"
Maker = async_sessionmaker[AsyncSession]
TransportLiteral = Literal["streamable-http", "sse"]


@pytest.fixture
async def maker() -> AsyncIterator[Maker]:
    engine = create_async_engine(TEST_DATABASE_URL)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


def spec(name: str, description: str = "upstream description") -> UpstreamToolSpec:
    return UpstreamToolSpec(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )


async def make_personal_service(
    db: AsyncSession, url: str | None = None
) -> tuple[Service, Connection]:
    from cam.accounts.users import create_user
    from cam.connections.orgs import ensure_org

    await ensure_org(db)
    owner = await create_user(
        db,
        email=f"resync-{uuid.uuid4().hex[:10]}@example.com",
        password="pw",
        role="user",
    )
    service, connection = await register_personal_mcp(
        db,
        owner=owner,
        slug=f"mine-{uuid.uuid4().hex[:8]}",
        name="Mine",
        url=url or "https://example.com/mcp",
        transport="streamable-http",
        auth_type="none",
    )
    return service, connection


async def allow_loopback(db: AsyncSession) -> None:
    from cam.connections.url_policy import add_url_allowlist

    try:
        await add_url_allowlist(db, pattern="127.0.0.0/8")
        await db.flush()
    except Exception:
        await db.rollback()


async def make_global_service(db: AsyncSession, url: str | None = None) -> Service:
    service, _connection = await register_global_mcp(
        db,
        slug=f"resync-{uuid.uuid4().hex[:8]}",
        name="Global",
        url=url or "https://example.com/mcp",
        transport="streamable-http",
        auth_type="none",
    )
    return service


def seed_tool(db: AsyncSession, service: Service, name: str) -> Tool:
    tool = Tool(
        org_id=service.org_id,
        service_id=service.id,
        name=name,
        upstream_name=name,
        description="seeded",
        input_schema={"type": "object", "properties": {}},
        enabled=True,
        status="active",
    )
    db.add(tool)
    return tool


async def tool_rows(db: AsyncSession, service: Service) -> dict[str, Tool]:
    rows = (await db.execute(select(Tool).where(Tool.service_id == service.id))).scalars()
    return {row.name: row for row in rows}


async def test_personal_new_tool_autos_enabled(maker: Maker) -> None:
    async with maker() as db:
        service, _connection = await make_personal_service(db)
        seed_tool(db, service, "echo")
        await db.commit()
    added, updated, removed = await apply_diff_now(maker, service, [spec("echo"), spec("new_one")])
    assert added == ["new_one"]
    assert updated == ["echo"]
    assert removed == []
    async with maker() as db:
        rows = await tool_rows(db, service)
        assert rows["new_one"].enabled is True
        assert rows["new_one"].status == "active"
        assert rows["echo"].status == "active"


async def apply_diff_now(
    maker: Maker, service: Service, specs: list[UpstreamToolSpec]
) -> tuple[list[str], list[str], list[str]]:
    async with maker() as db:
        result = await apply_diff(db, service, specs)
        await db.commit()
    return result


async def test_global_new_tool_pending_review(maker: Maker) -> None:
    async with maker() as db:
        service = await make_global_service(db)
        seed_tool(db, service, "echo")
        await db.commit()
    added, _updated, _removed = await apply_diff_now(maker, service, [spec("echo"), spec("fresh")])
    assert added == ["fresh"]
    async with maker() as db:
        rows = await tool_rows(db, service)
        assert rows["fresh"].status == "pending_review"
        assert rows["fresh"].enabled is False
        assert rows["echo"].status == "active"


async def test_missing_upstream_marks_removed_upstream(maker: Maker) -> None:
    async with maker() as db:
        service, _ = await make_personal_service(db)
        seed_tool(db, service, "ghost")
        await db.commit()
    _added, _updated, removed = await apply_diff_now(maker, service, [spec("echo")])
    assert removed == ["ghost"]
    async with maker() as db:
        rows = await tool_rows(db, service)
        assert "ghost" in rows
        assert rows["ghost"].status == "removed_upstream"


async def test_description_updated_but_override_wins(maker: Maker) -> None:
    async with maker() as db:
        service, _ = await make_personal_service(db)
        echo = seed_tool(db, service, "echo")
        echo.description_override = "owner's override"
        await db.flush()
        await db.commit()
    await apply_diff_now(maker, service, [spec("echo", "brand new description")])
    async with maker() as db:
        rows = await tool_rows(db, service)
        assert rows["echo"].description == "brand new description"
    await apply_diff_now(maker, service, [spec("echo", "ignored upstream text")])
    async with maker() as db:
        rows = await tool_rows(db, service)
        assert rows["echo"].description == "ignored upstream text"
    await apply_diff_now(maker, service, [spec("echo", "upstream text again")])
    async with maker() as db:
        rows = await tool_rows(db, service)
        assert rows["echo"].description_override == "owner's override"


async def test_override_setting_survives_upstream_refresh(maker: Maker) -> None:
    async with maker() as db:
        service, _ = await make_personal_service(db)
        echo = seed_tool(db, service, "echo")
        echo.description_override = "owner's override"
        await db.flush()
        await db.commit()
    changed = UpstreamToolSpec(
        name="echo",
        description="brand new description",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )
    await apply_diff_now(maker, service, [changed])
    async with maker() as db:
        rows = await tool_rows(db, service)
        assert rows["echo"].description_override == "owner's override"


async def test_removed_tool_comes_back_auto_enabled_for_personal(
    maker: Maker,
) -> None:
    async with maker() as db:
        service, _ = await make_personal_service(db)
        ghost = seed_tool(db, service, "ghost")
        ghost.status = "removed_upstream"
        await db.flush()
        await db.commit()
    _added, updated, _removed = await apply_diff_now(maker, service, [spec("echo"), spec("ghost")])
    async with maker() as db:
        rows = await tool_rows(db, service)
        assert rows["ghost"].status == "active"
        assert rows["ghost"].enabled is True
    del updated


async def test_removed_tool_comes_back_pending_review_for_global(maker: Maker) -> None:
    async with maker() as db:
        service = await make_global_service(db)
        ghost = seed_tool(db, service, "ghost")
        ghost.status = "removed_upstream"
        await db.flush()
        await db.commit()
    await apply_diff_now(maker, service, [spec("echo"), spec("ghost")])
    async with maker() as db:
        rows = await tool_rows(db, service)
        assert rows["ghost"].status == "pending_review"
        assert rows["ghost"].enabled is False


async def import_via_upstream(
    maker: Maker,
    *,
    service: Service,
    url: str,
    transport: TransportLiteral,
    names: set[str] | None = None,
) -> None:
    target = ConnectionTarget(
        service_id=str(service.id),
        connection_id=f"probe-{uuid.uuid4().hex[:8]}",
        url=url,
        transport=transport,
    )
    session = UpstreamSession(target, call_timeout_s=10.0)
    await session.start()
    try:
        tools = await session.list_tools()
    finally:
        await session.aclose()
    async with maker() as db:
        for tool in tools:
            if names is not None and tool.name not in names:
                continue
            db.add(
                Tool(
                    org_id=service.org_id,
                    service_id=service.id,
                    name=tool.name,
                    upstream_name=tool.name,
                    description=tool.description,
                    input_schema=dict(tool.input_schema),
                    enabled=True,
                    status="active",
                )
            )
        await db.commit()


async def wait_for(predicate: Callable[[], Awaitable[bool]], timeout_s: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.05)
    return await predicate()


async def test_upstream_change_updates_schema(maker: Maker) -> None:
    async with maker() as db:
        service, _ = await make_personal_service(db)
        tool = seed_tool(db, service, "echo")
        tool.input_schema = {"type": "object", "properties": {}}
        await db.flush()
        await db.commit()
    changed = UpstreamToolSpec(
        name="echo",
        description="new upstream description",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    await apply_diff_now(maker, service, [spec("other")])
    await apply_diff_now(maker, service, [changed, spec("other")])
    async with maker() as db:
        rows = await tool_rows(db, service)
        schema = rows["echo"].input_schema or {}
        assert schema["properties"]["q"] == {"type": "string"}
        assert rows["echo"].description == "new upstream description"


async def test_mcp_resync_full_cycle(maker: Maker, mock_mcp: MockMCPHandle) -> None:
    async with maker() as db:
        await allow_loopback(db)
        service, _connection = await make_personal_service(db, mock_mcp.streamable_url)
        await db.commit()
        await import_via_upstream(
            maker,
            service=service,
            url=mock_mcp.streamable_url,
            transport="streamable-http",
        )
    dynamic_name = f"fresh_{uuid.uuid4().hex[:8]}"
    probe = UpstreamSession(
        ConnectionTarget(
            service_id="probe",
            connection_id=uuid.uuid4().hex,
            url=mock_mcp.streamable_url,
            transport="streamable-http",
        ),
        call_timeout_s=10.0,
    )
    await probe.start()
    try:
        outcome = await probe.call_tool("add_dynamic_tool", {"name": dynamic_name})
        assert outcome.is_error is False
    finally:
        await probe.aclose()
    async with maker() as db:
        service = (await db.execute(select(Service).where(Service.id == service.id))).scalar_one()
        outcome = await resync_by_ref(db, service)
        await db.commit()
    assert outcome.status == "done", outcome.error
    assert dynamic_name in outcome.added
    async with maker() as db:
        rows = await tool_rows(db, service)
        assert rows[dynamic_name].enabled is True
        assert rows[dynamic_name].status == "active"


async def test_mcp_resync_global_new_tool_hidden(maker: Maker, mock_mcp: MockMCPHandle) -> None:
    async with maker() as db:
        service = await make_global_service(db, mock_mcp.streamable_url)
        await db.commit()
        await import_via_upstream(
            maker, service=service, url=mock_mcp.streamable_url, transport="streamable-http"
        )
    dynamic_name = f"hidden_{uuid.uuid4().hex[:8]}"
    probe = UpstreamSession(
        ConnectionTarget(
            service_id="probe2",
            connection_id=uuid.uuid4().hex,
            url=mock_mcp.streamable_url,
            transport="streamable-http",
        ),
        call_timeout_s=10.0,
    )
    await probe.start()
    try:
        await probe.call_tool("add_dynamic_tool", {"name": dynamic_name})
    finally:
        await probe.aclose()
    async with maker() as db:
        service = (await db.execute(select(Service).where(Service.id == service.id))).scalar_one()
        outcome = await resync_by_ref(db, service)
        await db.commit()
    assert outcome.status == "done"
    async with maker() as db:
        rows = await tool_rows(db, service)
        assert rows[dynamic_name].status == "pending_review"
        assert rows[dynamic_name].enabled is False


async def make_personal_service_rest(
    db: AsyncSession, mock_rest: MockRestHandle
) -> tuple[Service, Connection | None]:
    from cam.accounts.users import create_user
    from cam.connections.orgs import ensure_org
    from cam.registry.services import register_service

    await ensure_org(db)
    owner = await create_user(
        db,
        email=f"restsync-{uuid.uuid4().hex[:10]}@example.com",
        password="pw",
        role="user",
    )
    service, connection = await register_service(
        db,
        tier="personal",
        owner=owner,
        slug=f"restmine-{uuid.uuid4().hex[:8]}",
        name="REST Mine",
        url=mock_rest.base_url,
        kind="rest",
        auth_type="none",
        spec_url=mock_rest.spec_url,
    )
    return service, connection


def tool_status(rows: dict[str, Tool], name: str) -> tuple[str, bool]:
    row = rows[name]
    return row.status, row.enabled


async def test_coordinator_enqueues_and_resyncs(maker: Maker, mock_mcp: MockMCPHandle) -> None:
    from cam.registry.resync import ResyncCoordinator, lock_for, resync_service_id

    coordinator = ResyncCoordinator(maker, debounce_s=0.0)
    coordinator.start()
    try:
        async with maker() as db:
            service, _connection = await make_personal_service(db, mock_mcp.streamable_url)
            await db.commit()
        service_id = service.id
        await import_via_upstream(
            maker,
            service=service,
            url=mock_mcp.streamable_url,
            transport="streamable-http",
            names={"echo"},
        )

        async def listed() -> bool:
            async with maker() as db:
                rows = await tool_rows(db, service)
            return "echo" in rows

        coordinator.enqueue(service_id)
        assert await wait_for(listed)
        coordinator.enqueue(service_id)
        assert coordinator.pending_count() == 1
    finally:
        await coordinator.aclose()
    lock = lock_for(service_id)
    async with lock:
        outcome = await resync_service_id(maker, service_id)
        assert outcome.status == "skipped"


async def test_list_changed_handler_enqueues_service(maker: Maker, mock_mcp: MockMCPHandle) -> None:
    from cam.api.main import list_changed_handler_factory
    from cam.registry.resync import ResyncCoordinator

    coordinator = ResyncCoordinator(maker, debounce_s=0.0)
    try:
        factory = list_changed_handler_factory(coordinator)
        handler = factory(str(uuid.uuid4()), str(uuid.uuid4()))

        class ToolListChangedNotification:
            pass

        notification = ToolListChangedNotification()
        del ToolListChangedNotification
        await handler(notification)
        assert coordinator.pending_count() == 1
    finally:
        await coordinator.aclose()
    del mock_mcp


async def test_scheduled_resync_imports_every_active_service(
    maker: Maker, mock_mcp: MockMCPHandle
) -> None:
    from cam.jobs.resync import run_resync_all

    async with maker() as db:
        service, _connection = await make_personal_service(db, mock_mcp.streamable_url)
        await db.commit()
    await import_via_upstream(
        maker,
        service=service,
        url=mock_mcp.streamable_url,
        transport="streamable-http",
        names={"echo"},
    )
    done = await run_resync_all(maker)
    assert done >= 1
    async with maker() as db:
        rows = await tool_rows(db, service)
        assert "echo" in rows
        assert "add_dynamic_tool" in rows


async def test_rest_resync_refetches_spec(maker: Maker, mock_rest: MockRestHandle) -> None:
    from cam.registry.resync import resync_by_ref

    async with maker() as db:
        await allow_loopback(db)
        service, _connection = await make_personal_service_rest_typed(db, mock_rest)
        await db.commit()
    async with maker() as db:
        refreshed = (await db.execute(select(Service).where(Service.id == service.id))).scalar_one()
        outcome = await resync_by_ref(db, refreshed)
        await db.commit()
    assert outcome.status == "done", outcome.error
    assert "boom" in outcome.added
    assert "whoami" not in outcome.added
    async with maker() as db:
        rows = await tool_rows(db, service)
        assert tool_status(rows, "whoami") == ("active", True)
        assert tool_status(rows, "boom") == ("active", True)


async def make_personal_service_rest_typed(
    db: AsyncSession, mock_rest: MockRestHandle
) -> tuple[Service, Connection]:
    from cam.accounts.users import create_user
    from cam.connections.orgs import ensure_org
    from cam.registry.services import register_service

    await ensure_org(db)
    owner = await create_user(
        db,
        email=f"restsync2-{uuid.uuid4().hex[:10]}@example.com",
        password="pw",
        role="user",
    )
    service, connection = await register_service(
        db,
        tier="personal",
        owner=owner,
        slug=f"restmine2-{uuid.uuid4().hex[:8]}",
        name="REST Mine Two",
        url=mock_rest.base_url,
        kind="rest",
        auth_type="none",
        spec_url=mock_rest.spec_url,
        tools=["whoami"],
    )
    assert connection is not None
    return service, connection
