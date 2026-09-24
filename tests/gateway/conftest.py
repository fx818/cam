"""Fixtures for the CAM MCP gateway (/mcp) tests."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from cam.api.main import create_app
from cam.config import Settings
from cam.connections.orgs import ensure_org
from cam.connections.tokens import create_token
from cam.db.models import ServiceVisibility, Tool, User
from cam.db.session import create_db_engine, create_session_factory
from cam.registry.services import McpTransport, register_global_mcp
from cam.upstream import ConnectionTarget, UpstreamSession
from fixtures.mock_mcp import MockMCPHandle
from gateway.helpers import GatewayData, GatewayEnv, McpConnection


async def _import_tools(
    session: Any,
    *,
    org_id: uuid.UUID,
    service: Any,
    url: str,
    transport: McpTransport,
    headers: dict[str, str] | None = None,
) -> list[Tool]:
    target = ConnectionTarget(
        service_id=str(service.id),
        connection_id=f"import-{uuid.uuid4().hex[:8]}",
        url=url,
        transport=transport,
        headers=headers or {},
    )
    upstream = UpstreamSession(target, call_timeout_s=15.0)
    await upstream.start()
    try:
        upstream_tools = await upstream.list_tools()
    finally:
        await upstream.aclose()
    rows = [
        Tool(
            org_id=org_id,
            service_id=service.id,
            name=tool.name,
            upstream_name=tool.name,
            description=tool.description,
            input_schema=tool.input_schema,
            enabled=True,
            status="active",
        )
        for tool in upstream_tools
    ]
    session.add_all(rows)
    await session.flush()
    return rows


def _make_user(org_id: uuid.UUID, suffix: str, label: str) -> User:
    return User(
        org_id=org_id,
        email=f"gw-{label}-{suffix}@example.com",
        handle=f"gw{label}{suffix}",
        display_name=f"Gateway {label}",
        password_hash="x",
        role="user",
        kind="person",
    )


@pytest.fixture(scope="session")
async def gateway_data(
    mock_mcp: MockMCPHandle, mock_mcp_authed: MockMCPHandle
) -> AsyncIterator[GatewayData]:
    engine = create_db_engine(Settings().database_url)
    factory = create_session_factory(engine)
    suffix = uuid.uuid4().hex[:12]
    try:
        async with factory() as session:
            org = await ensure_org(session)
            user_a = _make_user(org.id, suffix, "a")
            user_b = _make_user(org.id, suffix, "b")
            session.add_all([user_a, user_b])
            await session.flush()

            raw_a, _ = await create_token(
                session, org_id=org.id, user_id=user_a.id, label="gateway-a"
            )
            raw_b, _ = await create_token(
                session, org_id=org.id, user_id=user_b.id, label="gateway-b"
            )
            raw_revoked, revoked = await create_token(
                session, org_id=org.id, user_id=user_a.id, label="gateway-revoked"
            )
            revoked.revoked_at = datetime.now(UTC)

            service_a, _ = await register_global_mcp(
                session,
                slug=f"gwstream{suffix}",
                name="Gateway Stream Mock",
                url=mock_mcp.streamable_url,
                transport="streamable-http",
                auth_type="none",
                description="Mock streamable service for gateway tests",
            )
            service_b, _ = await register_global_mcp(
                session,
                slug=f"gwsse{suffix}",
                name="Gateway SSE Mock",
                url=mock_mcp.sse_url,
                transport="sse",
                auth_type="none",
                description="Mock SSE service for gateway tests",
            )
            service_b.visibility = "restricted"
            session.add(
                ServiceVisibility(org_id=org.id, service_id=service_b.id, user_id=user_b.id)
            )

            service_timeout, _ = await register_global_mcp(
                session,
                slug=f"gwtimeout{suffix}",
                name="Gateway Timeout Mock",
                url=mock_mcp.streamable_url,
                transport="streamable-http",
                auth_type="none",
                description="Service with a very small per-service timeout",
            )
            service_timeout.timeout_ms = 500

            service_auth_ok, _ = await register_global_mcp(
                session,
                slug=f"gwauthok{suffix}",
                name="Gateway Auth OK",
                url=mock_mcp_authed.streamable_url,
                transport="streamable-http",
                auth_type="bearer",
                credential="test-secret",
                description="Bearer-authenticated mock with the right secret",
            )
            service_auth_bad, _ = await register_global_mcp(
                session,
                slug=f"gwauthbad{suffix}",
                name="Gateway Auth Bad",
                url=mock_mcp_authed.streamable_url,
                transport="streamable-http",
                auth_type="bearer",
                credential="wrong-secret",
                description="Bearer-authenticated mock with a stale secret",
            )
            service_dead, _ = await register_global_mcp(
                session,
                slug=f"gwdead{suffix}",
                name="Gateway Dead Mock",
                url="http://127.0.0.1:9/mcp",
                transport="streamable-http",
                auth_type="none",
                description="Nothing listens on this address",
            )

            auth_headers = {"Authorization": "Bearer test-secret"}
            tools_a = await _import_tools(
                session,
                org_id=org.id,
                service=service_a,
                url=mock_mcp.streamable_url,
                transport="streamable-http",
            )
            await _import_tools(
                session,
                org_id=org.id,
                service=service_b,
                url=mock_mcp.sse_url,
                transport="sse",
            )
            await _import_tools(
                session,
                org_id=org.id,
                service=service_timeout,
                url=mock_mcp.streamable_url,
                transport="streamable-http",
            )
            await _import_tools(
                session,
                org_id=org.id,
                service=service_auth_ok,
                url=mock_mcp_authed.streamable_url,
                transport="streamable-http",
                headers=auth_headers,
            )
            await _import_tools(
                session,
                org_id=org.id,
                service=service_auth_bad,
                url=mock_mcp_authed.streamable_url,
                transport="streamable-http",
                headers=auth_headers,
            )
            session.add(
                Tool(
                    org_id=org.id,
                    service_id=service_dead.id,
                    name="echo",
                    upstream_name="echo",
                    description="Echo text back unchanged",
                    input_schema={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    enabled=True,
                    status="active",
                )
            )
            disabled = next(tool for tool in tools_a if tool.name == "add_dynamic_tool")
            disabled.enabled = False
            await session.commit()

            yield GatewayData(
                token_a=raw_a,
                token_b=raw_b,
                token_revoked=raw_revoked,
                service_a_ref=f"global/{service_a.slug}",
                service_b_ref=f"global/{service_b.slug}",
                service_a_slug=service_a.slug,
                service_b_slug=service_b.slug,
                echo_tool_id=f"global/{service_a.slug}/echo",
                disabled_tool_id=f"global/{service_a.slug}/add_dynamic_tool",
                timeout_ref=f"global/{service_timeout.slug}",
                auth_ok_ref=f"global/{service_auth_ok.slug}",
                auth_bad_ref=f"global/{service_auth_bad.slug}",
                dead_ref=f"global/{service_dead.slug}",
            )
    finally:
        await engine.dispose()


@asynccontextmanager
async def _app_env(app: FastAPI, data: GatewayData) -> AsyncGenerator[GatewayEnv]:
    """Run the app lifespan in a dedicated task.

    pytest-asyncio finalizes async-generator fixtures from a different task
    than the one that set them up, and the MCP session manager hosts an anyio
    cancel scope that must be entered and exited in the same task. Driving the
    lifespan here keeps enter/exit inside one task; teardown only signals stop
    and awaits a clean shutdown.
    """
    stop = asyncio.Event()
    started = asyncio.Event()
    failure: list[BaseException] = []

    async def _run_lifespan() -> None:
        try:
            async with app.router.lifespan_context(app):
                started.set()
                await stop.wait()
        except BaseException as exc:
            failure.append(exc)
            started.set()
            raise

    task = asyncio.create_task(_run_lifespan(), name="cam-test-lifespan")
    await started.wait()
    if failure:
        await task
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway.test", timeout=30.0
    ) as client:
        yield GatewayEnv(app=app, client=client, data=data)
    stop.set()
    await task


@pytest.fixture
async def gateway(gateway_data: GatewayData) -> AsyncIterator[GatewayEnv]:
    async with _app_env(create_app(), gateway_data) as env:
        yield env


@pytest.fixture
async def gateway_limited(gateway_data: GatewayData) -> AsyncIterator[GatewayEnv]:
    app = create_app(Settings(rate_limit_per_min=2))
    async with _app_env(app, gateway_data) as env:
        yield env


@pytest.fixture
async def mcp_a(gateway: GatewayEnv) -> McpConnection:
    connection = McpConnection(gateway.client, gateway.data.token_a)
    await connection.initialize()
    return connection


@pytest.fixture
async def mcp_b(gateway: GatewayEnv) -> McpConnection:
    connection = McpConnection(gateway.client, gateway.data.token_b)
    await connection.initialize()
    return connection
