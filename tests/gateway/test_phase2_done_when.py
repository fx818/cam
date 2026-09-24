"""Phase 2 done-when, end to end through /mcp.

User B uses A's personal service via a share; access disappears the moment
the share is removed or A is deactivated; a stranger never sees the service.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from cam.access import add_share, revoke_share
from cam.accounts.users import deactivate_user
from cam.api.main import create_app
from cam.config import Settings
from cam.connections.orgs import ensure_org
from cam.connections.tokens import create_token
from cam.connections.url_policy import add_url_allowlist
from cam.db.models import Tool, User
from cam.db.session import create_db_engine, create_session_factory
from cam.registry.services import register_personal_mcp
from cam.upstream import ConnectionTarget, UpstreamSession
from fixtures.mock_mcp import MockMCPHandle
from gateway.helpers import McpConnection, error_payload, make_result_payload, make_result_text

OWNER_MESSAGE = "This service is no longer available: its owner's account is deactivated."


def _user(org_id: uuid.UUID, suffix: str, role: str) -> User:
    return User(
        org_id=org_id,
        email=f"dw{role}-{suffix}@example.com",
        handle=f"dw{role}{suffix}",
        display_name=f"DoneWhen {role.upper()}",
        password_hash="x",
        role="user",
        kind="person",
    )


@asynccontextmanager
async def _running_app(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    stop = asyncio.Event()
    started = asyncio.Event()
    failures: list[BaseException] = []

    async def _run_lifespan() -> None:
        try:
            async with app.router.lifespan_context(app):
                started.set()
                await stop.wait()
        except BaseException as exc:
            failures.append(exc)
            started.set()
            raise

    task = asyncio.create_task(_run_lifespan(), name="cam-done-when-lifespan")
    await started.wait()
    if failures:
        await task
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway.test", timeout=30.0
    ) as client:
        yield client
    stop.set()
    await task


async def _service_ids(connection: McpConnection) -> list[str]:
    result = await connection.call({"action": "list_services"})
    assert result["isError"] is False, result
    payload = make_result_payload(result)
    return [service["id"] for service in payload["services"]]


async def test_phase2_done_when(mock_mcp: MockMCPHandle) -> None:
    suffix = uuid.uuid4().hex[:10]
    org_slug = f"donewhen-{suffix}"
    engine = create_db_engine(Settings().database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            org = await ensure_org(session, slug=org_slug)
            await add_url_allowlist(session, pattern="127.0.0.0/8", org_slug=org_slug)
            user_a = _user(org.id, suffix, "a")
            user_b = _user(org.id, suffix, "b")
            stranger = _user(org.id, suffix, "s")
            session.add_all([user_a, user_b, stranger])
            await session.flush()
            raw_b, _token_b = await create_token(
                session, org_id=org.id, user_id=user_b.id, label="dw-b"
            )
            raw_s, _token_s = await create_token(
                session, org_id=org.id, user_id=stranger.id, label="dw-s"
            )

            service, connection = await register_personal_mcp(
                session,
                owner=user_a,
                slug=f"shared{suffix}",
                name="A's Shared MCP",
                url=mock_mcp.streamable_url,
                transport="streamable-http",
                auth_type="none",
            )
            upstream = UpstreamSession(
                ConnectionTarget(
                    service_id=str(service.id),
                    connection_id=f"import-{uuid.uuid4().hex[:8]}",
                    url=mock_mcp.streamable_url,
                    transport="streamable-http",
                ),
                call_timeout_s=15.0,
            )
            await upstream.start()
            try:
                upstream_tools = await upstream.list_tools()
            finally:
                await upstream.aclose()
            echo = next(tool for tool in upstream_tools if tool.name == "echo")
            session.add(
                Tool(
                    org_id=org.id,
                    service_id=service.id,
                    name="echo",
                    upstream_name="echo",
                    description=echo.description,
                    input_schema=echo.input_schema,
                    enabled=True,
                    status="active",
                )
            )
            await session.flush()
            share = await add_share(
                session, connection_id=connection.id, grantee_user_id=user_b.id, actor=user_a
            )
            ref = f"{user_a.handle}/{service.slug}"
            tool_id = f"{ref}/echo"
            await session.commit()

        app = create_app(Settings())
        async with _running_app(app) as client:
            mcp_b = McpConnection(client, raw_b)
            await mcp_b.initialize()
            mcp_s = McpConnection(client, raw_s)
            await mcp_s.initialize()

            assert ref in await _service_ids(mcp_b)
            listed_tools = await mcp_b.call({"action": "list_tools", "service": ref})
            assert listed_tools["isError"] is False, listed_tools

            invoked = await mcp_b.call(
                {"action": "invoke", "id": tool_id, "args": {"text": "shared-ok"}}
            )
            assert invoked["isError"] is False, invoked
            assert make_result_text(invoked) == "shared-ok"

            assert ref not in await _service_ids(mcp_s)
            stranger_describe = error_payload(
                await mcp_s.call({"action": "describe", "id": tool_id})
            )
            assert stranger_describe["code"] == "NOT_FOUND"
            stranger_invoke = error_payload(
                await mcp_s.call({"action": "invoke", "id": tool_id, "args": {"text": "x"}})
            )
            assert stranger_invoke["code"] == "NOT_FOUND"
            stranger_list = error_payload(
                await mcp_s.call({"action": "list_tools", "service": ref})
            )
            assert stranger_list["code"] == "NOT_FOUND"

            async with factory() as session:
                owner_row = await session.get(User, user_a.id)
                assert owner_row is not None
                await revoke_share(session, share_id=share.id, actor=owner_row)
                await session.commit()

            revoked_invoke = error_payload(
                await mcp_b.call(
                    {"action": "invoke", "id": tool_id, "args": {"text": "after-revoke"}}
                )
            )
            assert revoked_invoke["code"] == "NOT_FOUND"
            assert revoked_invoke["message"] == "Tool not found or not available."
            assert ref not in await _service_ids(mcp_b)
            revoked_describe = error_payload(
                await mcp_b.call({"action": "describe", "id": tool_id})
            )
            assert revoked_describe["code"] == "NOT_FOUND"

            async with factory() as session:
                owner_row = await session.get(User, user_a.id)
                assert owner_row is not None
                await add_share(
                    session,
                    connection_id=connection.id,
                    grantee_user_id=user_b.id,
                    actor=owner_row,
                )
                await session.commit()

            reshared = await mcp_b.call(
                {"action": "invoke", "id": tool_id, "args": {"text": "reshared-ok"}}
            )
            assert reshared["isError"] is False, reshared
            assert make_result_text(reshared) == "reshared-ok"

            async with factory() as session:
                await deactivate_user(session, email=f"dwa-{suffix}@example.com")
                await session.commit()

            deactivated_invoke = error_payload(
                await mcp_b.call(
                    {"action": "invoke", "id": tool_id, "args": {"text": "after-deactivate"}}
                )
            )
            assert deactivated_invoke["code"] == "OWNER_DEACTIVATED"
            assert deactivated_invoke["message"] == OWNER_MESSAGE
            assert deactivated_invoke["service"] == ref
            assert deactivated_invoke["retryable"] is False

            deactivated_describe = error_payload(
                await mcp_b.call({"action": "describe", "id": tool_id})
            )
            assert deactivated_describe["code"] == "OWNER_DEACTIVATED"
            assert deactivated_describe["message"] == OWNER_MESSAGE
            assert ref not in await _service_ids(mcp_b)

            stranger_after = error_payload(await mcp_s.call({"action": "describe", "id": tool_id}))
            assert stranger_after["code"] == "NOT_FOUND"
            assert ref not in await _service_ids(mcp_s)
    finally:
        await engine.dispose()
