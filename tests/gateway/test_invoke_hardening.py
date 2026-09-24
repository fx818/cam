"""Phase 1.6 invoke hardening: timeout, truncation, credentials, rate limit."""

from __future__ import annotations

import pytest

from fixtures.mock_mcp import MockMCPHandle
from gateway.helpers import GatewayEnv, McpConnection, error_payload, make_result_text


async def _connection(env: GatewayEnv) -> McpConnection:
    connection = McpConnection(env.client, env.data.token_a)
    await connection.initialize()
    return connection


async def test_per_service_timeout_maps_to_timeout(
    gateway: GatewayEnv, mock_mcp: MockMCPHandle
) -> None:
    connection = await _connection(gateway)
    mock_mcp.call_counts.clear()
    result = await connection.call(
        {
            "action": "invoke",
            "id": f"{gateway.data.timeout_ref}/slow",
            "args": {"sleep_s": 5.0},
        }
    )
    error = error_payload(result)
    assert error["code"] == "TIMEOUT"
    assert "is unavailable or timed out" in error["message"]
    assert "Gateway Timeout Mock" in error["message"]
    assert error["service"] == gateway.data.timeout_ref
    assert error["retryable"] is True
    assert mock_mcp.call_counts.get("slow", 0) == 1


async def test_truncates_huge_plain_text(gateway: GatewayEnv) -> None:
    connection = await _connection(gateway)
    tool_id = f"{gateway.data.service_a_ref}/huge"
    result = await connection.call({"action": "invoke", "id": tool_id, "args": {"chars": 400_000}})
    assert result["isError"] is False
    text = make_result_text(result)
    assert len(text) <= 200_000
    assert "[CAM: response truncated" in text
    assert "showing the first ~50000 tokens" in text


async def test_truncates_huge_json_array(gateway: GatewayEnv) -> None:
    connection = await _connection(gateway)
    tool_id = f"{gateway.data.service_a_ref}/huge"
    result = await connection.call(
        {"action": "invoke", "id": tool_id, "args": {"chars": 200_000, "as_json": True}}
    )
    assert result["isError"] is False
    text = make_result_text(result)
    assert len(text) <= 200_000
    assert "[CAM: response truncated" in text
    assert "items (~50000 token cap)" in text


async def test_bearer_credential_injected(gateway: GatewayEnv) -> None:
    connection = await _connection(gateway)
    tool_id = f"{gateway.data.auth_ok_ref}/echo"
    result = await connection.call(
        {"action": "invoke", "id": tool_id, "args": {"text": "authed-ok"}}
    )
    assert result["isError"] is False
    assert make_result_text(result) == "authed-ok"


async def test_bad_credential_maps_to_upstream_auth(gateway: GatewayEnv) -> None:
    connection = await _connection(gateway)
    tool_id = f"{gateway.data.auth_bad_ref}/echo"
    result = await connection.call({"action": "invoke", "id": tool_id, "args": {"text": "denied"}})
    error = error_payload(result)
    assert error["code"] == "UPSTREAM_AUTH"
    assert error["message"] == "Credential rejected. Owner needs to reconnect."
    assert error["service"] == gateway.data.auth_bad_ref
    assert error["retryable"] is False


async def test_dead_endpoint_maps_to_unavailable(gateway: GatewayEnv) -> None:
    connection = await _connection(gateway)
    tool_id = f"{gateway.data.dead_ref}/echo"
    result = await connection.call(
        {"action": "invoke", "id": tool_id, "args": {"text": "nobody-home"}}
    )
    error = error_payload(result)
    assert error["code"] == "UPSTREAM_UNAVAILABLE"
    assert "is unavailable or timed out" in error["message"]
    assert "Gateway Dead Mock" in error["message"]
    assert error["retryable"] is True


async def test_rate_limit_blocks_third_invoke(gateway_limited: GatewayEnv) -> None:
    connection = await _connection(gateway_limited)
    tool_id = f"{gateway_limited.data.echo_tool_id}"
    args = {"action": "invoke", "id": tool_id, "args": {"text": "one"}}

    first = await connection.call(args)
    assert first["isError"] is False
    second = await connection.call(args)
    assert second["isError"] is False
    third = await connection.call(args)
    error = error_payload(third)
    assert error["code"] == "CAM_RATE_LIMITED"
    assert "invoke calls per minute" in error["message"]
    assert error["retryable"] is True
    assert error["service"] is None

    listed = await connection.call({"action": "list_services"})
    assert listed["isError"] is False


async def test_validation_rejects_missing_argument(gateway: GatewayEnv) -> None:
    connection = await _connection(gateway)
    tool_id = f"{gateway.data.echo_tool_id}"
    result = await connection.call({"action": "invoke", "id": tool_id, "args": {}})
    error = error_payload(result)
    assert error["code"] == "INVALID_ARGS"
    assert "text" in error["message"]
    assert "required" in error["message"]
    assert error["service"] is None


async def test_validation_rejects_wrong_type(gateway: GatewayEnv) -> None:
    connection = await _connection(gateway)
    tool_id = f"{gateway.data.echo_tool_id}"
    result = await connection.call({"action": "invoke", "id": tool_id, "args": {"text": 123}})
    error = error_payload(result)
    assert error["code"] == "INVALID_ARGS"
    assert "text" in error["message"]
    assert "not of type" in error["message"]


@pytest.mark.parametrize("action", ["list_services", "describe"])
async def test_non_invoke_actions_do_not_consume_budget(
    gateway_limited: GatewayEnv, action: str
) -> None:
    connection = await _connection(gateway_limited)
    for _ in range(4):
        if action == "describe":
            result = await connection.call(
                {"action": action, "id": gateway_limited.data.echo_tool_id}
            )
        else:
            result = await connection.call({"action": action})
        assert result["isError"] is False
