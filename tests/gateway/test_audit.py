"""Phase 1.7: every cam tool call lands in audit_logs with full context."""

from __future__ import annotations

import uuid
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cam.audit.writer import AuditWriter
from cam.connections.tokens import lookup_token
from cam.db.models import AuditLog
from gateway.helpers import GatewayEnv, McpConnection, error_payload


async def _connection(env: GatewayEnv) -> McpConnection:
    connection = McpConnection(env.client, env.data.token_a)
    await connection.initialize()
    return connection


def _factory(env: GatewayEnv) -> async_sessionmaker[AsyncSession]:
    return cast("async_sessionmaker[AsyncSession]", env.app.state.session_factory)


async def _audit_ids(env: GatewayEnv, token_raw: str) -> set[uuid.UUID]:
    async with _factory(env)() as session:
        token = await lookup_token(session, token_raw)
        assert token is not None
        result = await session.execute(select(AuditLog.id).where(AuditLog.token_id == token.id))
        return set(result.scalars())


async def _new_rows(env: GatewayEnv, token_raw: str, before: set[uuid.UUID]) -> list[AuditLog]:
    audit = cast("AuditWriter", env.app.state.audit)
    await audit.flush()
    async with _factory(env)() as session:
        token = await lookup_token(session, token_raw)
        assert token is not None
        result = await session.execute(
            select(AuditLog).where(AuditLog.token_id == token.id).order_by(AuditLog.ts)
        )
        return [row for row in result.scalars() if row.id not in before]


async def test_invoke_ok_writes_full_audit_row(gateway: GatewayEnv) -> None:
    before = await _audit_ids(gateway, gateway.data.token_a)
    connection = await _connection(gateway)
    result = await connection.call(
        {"action": "invoke", "id": gateway.data.echo_tool_id, "args": {"text": "audit-ok"}}
    )
    assert result["isError"] is False
    rows = await _new_rows(gateway, gateway.data.token_a, before)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "invoke"
    assert row.status == "ok"
    assert row.error_code is None
    assert row.agent_name == "claude-code"
    assert row.agent_version == "1.0.0"
    assert row.tool_name == "echo"
    assert row.service_id is not None
    assert row.connection_id is not None
    assert row.user_id is not None
    assert row.token_id is not None
    assert row.org_id is not None
    assert row.args == {"text": "audit-ok"}
    assert row.latency_ms is not None
    assert row.latency_ms >= 0
    assert row.response_bytes is not None
    assert row.response_bytes > 0
    assert row.truncated is False
    assert row.ts is not None


async def test_list_and_describe_and_error_rows(gateway: GatewayEnv) -> None:
    before = await _audit_ids(gateway, gateway.data.token_a)
    connection = await _connection(gateway)

    listed = await connection.call({"action": "list_services"})
    assert listed["isError"] is False
    described = await connection.call({"action": "describe", "id": gateway.data.echo_tool_id})
    assert described["isError"] is False
    failed = await connection.call({"action": "invoke", "id": gateway.data.echo_tool_id})
    error = error_payload(failed)
    assert error["code"] == "INVALID_ARGS"
    missing = await connection.call(
        {"action": "describe", "id": f"{gateway.data.service_a_ref}/no-such"}
    )
    assert error_payload(missing)["code"] == "NOT_FOUND"

    rows = await _new_rows(gateway, gateway.data.token_a, before)
    assert len(rows) == 4

    list_row = next(r for r in rows if r.action == "list_services")
    assert list_row.status == "ok"
    assert list_row.args is None
    assert list_row.service_id is None

    describe_row = next(r for r in rows if r.action == "describe" and r.status == "ok")
    assert describe_row.tool_name == "echo"
    assert describe_row.service_id is not None
    assert describe_row.args is None

    invalid = next(r for r in rows if r.error_code == "INVALID_ARGS")
    assert invalid.status == "error"
    assert invalid.args == {}
    assert invalid.error_message

    not_found = next(r for r in rows if r.error_code == "NOT_FOUND")
    assert not_found.status == "error"
    assert not_found.service_id is None
    assert not_found.tool_name is None
    assert not_found.args is None


async def test_truncated_invoke_marks_truncated(gateway: GatewayEnv) -> None:
    before = await _audit_ids(gateway, gateway.data.token_a)
    connection = await _connection(gateway)
    result = await connection.call(
        {
            "action": "invoke",
            "id": f"{gateway.data.service_a_ref}/huge",
            "args": {"chars": 400_000},
        }
    )
    assert result["isError"] is False
    rows = await _new_rows(gateway, gateway.data.token_a, before)
    assert len(rows) == 1
    assert rows[0].truncated is True
    assert rows[0].status == "ok"
    assert rows[0].args == {"chars": 400_000}


async def test_rate_limited_invoke_writes_error_row(gateway_limited: GatewayEnv) -> None:
    before = await _audit_ids(gateway_limited, gateway_limited.data.token_a)
    connection = await _connection(gateway_limited)
    tool_id = gateway_limited.data.echo_tool_id
    args = {"action": "invoke", "id": tool_id, "args": {"text": "rl"}}
    for _ in range(2):
        assert (await connection.call(args))["isError"] is False
    third = await connection.call(args)
    assert error_payload(third)["code"] == "CAM_RATE_LIMITED"
    rows = await _new_rows(gateway_limited, gateway_limited.data.token_a, before)
    assert len(rows) == 3
    limited = [r for r in rows if r.error_code == "CAM_RATE_LIMITED"]
    assert len(limited) == 1
    row = limited[0]
    assert row.status == "error"
    assert row.service_id is None
    assert row.args == {"text": "rl"}


async def test_invoke_args_never_contain_secrets(gateway: GatewayEnv) -> None:
    before = await _audit_ids(gateway, gateway.data.token_a)
    connection = await _connection(gateway)
    tool_id = f"{gateway.data.auth_ok_ref}/echo"
    result = await connection.call(
        {"action": "invoke", "id": tool_id, "args": {"text": "secret-free"}}
    )
    assert result["isError"] is False
    rows = await _new_rows(gateway, gateway.data.token_a, before)
    assert len(rows) == 1
    assert rows[0].args == {"text": "secret-free"}
    assert "test-secret" not in str(rows[0].args)
    assert "Authorization" not in str(rows[0].args)
