"""End-to-end tests for the CAM MCP gateway at /mcp."""

from __future__ import annotations

from cam.gateway.server import CAM_TOOL_INPUT_SCHEMA, DESCRIPTION_FIRST_LINE, SERVER_INSTRUCTIONS
from gateway.helpers import (
    GatewayEnv,
    McpConnection,
    error_payload,
    make_result_payload,
    make_result_text,
)

EXPECTED_ACTIONS = {
    "list_services",
    "list_tools",
    "describe",
    "invoke",
    "list_skills",
    "load_skill",
    "read_skill_file",
}

UNAUTHORIZED_HEADERS = {"content-type": "application/json"}


async def test_missing_token_rejected(gateway: GatewayEnv) -> None:
    response = await gateway.client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers=UNAUTHORIZED_HEADERS,
    )
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"
    body = response.json()
    assert body["error"]["code"] == "UNAUTHORIZED"
    assert body["error"]["message"]


async def test_garbage_token_rejected(gateway: GatewayEnv) -> None:
    response = await gateway.client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={
            **UNAUTHORIZED_HEADERS,
            "authorization": "Bearer cam_not-a-real-token",
        },
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_revoked_token_rejected(gateway: GatewayEnv) -> None:
    response = await gateway.client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={
            **UNAUTHORIZED_HEADERS,
            "authorization": f"Bearer {gateway.data.token_revoked}",
        },
    )
    assert response.status_code == 401


async def test_initialize_returns_instructions_and_session(gateway: GatewayEnv) -> None:
    connection = McpConnection(gateway.client, gateway.data.token_a)
    message = await connection.initialize()
    result = message["result"]
    assert result["serverInfo"]["name"] == "cam"
    assert result["instructions"] == SERVER_INSTRUCTIONS
    assert "tools" in result["capabilities"]
    assert connection.session_id


async def test_tools_list_cam_schema_and_per_user_description(
    mcp_a: McpConnection, gateway: GatewayEnv
) -> None:
    result = await mcp_a.result("tools/list")
    tools = {tool["name"]: tool for tool in result["tools"]}
    assert "cam" in tools
    cam = tools["cam"]
    schema = cam["inputSchema"]
    assert schema["required"] == ["action"]
    assert set(schema["properties"]["action"]["enum"]) == EXPECTED_ACTIONS
    assert set(schema["properties"]) == set(CAM_TOOL_INPUT_SCHEMA["properties"])
    description = cam["description"]
    assert description.startswith(DESCRIPTION_FIRST_LINE)
    assert gateway.data.service_a_ref in description
    assert gateway.data.service_b_ref not in description


async def test_tools_list_description_includes_restricted_for_user_b(
    mcp_b: McpConnection, gateway: GatewayEnv
) -> None:
    result = await mcp_b.result("tools/list")
    cam = next(tool for tool in result["tools"] if tool["name"] == "cam")
    description = cam["description"]
    assert gateway.data.service_a_ref in description
    assert gateway.data.service_b_ref in description


async def test_list_services_user_a(mcp_a: McpConnection, gateway: GatewayEnv) -> None:
    result = await mcp_a.call({"action": "list_services"})
    assert result["isError"] is False
    payload = make_result_payload(result)
    assert payload == result["structuredContent"]
    ids = [service["id"] for service in payload["services"]]
    assert gateway.data.service_a_ref in ids
    assert gateway.data.service_b_ref not in ids
    stream = next(s for s in payload["services"] if s["id"] == gateway.data.service_a_ref)
    assert stream["tool_count"] >= 1


async def test_list_services_user_b_sees_restricted(
    mcp_b: McpConnection, gateway: GatewayEnv
) -> None:
    result = await mcp_b.call({"action": "list_services"})
    payload = make_result_payload(result)
    ids = [service["id"] for service in payload["services"]]
    assert gateway.data.service_b_ref in ids


async def test_list_tools_action(mcp_a: McpConnection, gateway: GatewayEnv) -> None:
    result = await mcp_a.call({"action": "list_tools", "service": gateway.data.service_a_ref})
    assert result["isError"] is False
    payload = make_result_payload(result)
    assert payload["service"] == gateway.data.service_a_ref
    tools = {tool["id"].rsplit("/", 1)[-1] for tool in payload["tools"]}
    assert {"echo", "fail", "image"} <= tools
    assert "add_dynamic_tool" not in tools


async def test_list_tools_without_service_invalid_args(mcp_a: McpConnection) -> None:
    result = await mcp_a.call({"action": "list_tools"})
    error = error_payload(result)
    assert error["code"] == "INVALID_ARGS"
    assert "service" in error["message"]
    assert error["service"] is None


async def test_describe_echo(mcp_a: McpConnection, gateway: GatewayEnv) -> None:
    result = await mcp_a.call({"action": "describe", "id": gateway.data.echo_tool_id})
    assert result["isError"] is False
    payload = make_result_payload(result)
    assert payload["id"] == gateway.data.echo_tool_id
    assert payload["description"]
    assert "properties" in payload["input_schema"]
    assert "text" in payload["input_schema"]["properties"]


async def test_describe_unknown_tool_not_found(mcp_a: McpConnection, gateway: GatewayEnv) -> None:
    result = await mcp_a.call(
        {"action": "describe", "id": f"{gateway.data.service_a_ref}/no-such-tool"}
    )
    error = error_payload(result)
    assert error["code"] == "NOT_FOUND"
    assert error["message"] == "Tool not found or not available."
    assert error["service"] is None
    assert error["retryable"] is False


async def test_describe_restricted_service_not_found_for_user_a(
    mcp_a: McpConnection, gateway: GatewayEnv
) -> None:
    result = await mcp_a.call({"action": "describe", "id": f"{gateway.data.service_b_ref}/echo"})
    error = error_payload(result)
    assert error["code"] == "NOT_FOUND"
    assert error["message"] == "Tool not found or not available."


async def test_describe_disabled_tool_not_found(mcp_a: McpConnection, gateway: GatewayEnv) -> None:
    result = await mcp_a.call({"action": "describe", "id": gateway.data.disabled_tool_id})
    error = error_payload(result)
    assert error["code"] == "NOT_FOUND"


async def test_invoke_echo_roundtrip(mcp_a: McpConnection, gateway: GatewayEnv) -> None:
    result = await mcp_a.call(
        {"action": "invoke", "id": gateway.data.echo_tool_id, "args": {"text": "ping-123"}}
    )
    assert result["isError"] is False
    assert make_result_text(result) == "ping-123"


async def test_invoke_structured_echo(mcp_a: McpConnection, gateway: GatewayEnv) -> None:
    tool_id = f"{gateway.data.service_a_ref}/structured_echo"
    result = await mcp_a.call({"action": "invoke", "id": tool_id, "args": {"value": {"a": 1}}})
    assert result["isError"] is False
    assert result["structuredContent"] == {"echo": {"a": 1}}


async def test_invoke_image(mcp_a: McpConnection, gateway: GatewayEnv) -> None:
    tool_id = f"{gateway.data.service_a_ref}/image"
    result = await mcp_a.call({"action": "invoke", "id": tool_id, "args": {}})
    assert result["isError"] is False
    kinds = {block.get("type") for block in result["content"]}
    assert "image" in kinds


async def test_invoke_fail_returns_upstream_error_envelope(
    mcp_a: McpConnection, gateway: GatewayEnv
) -> None:
    tool_id = f"{gateway.data.service_a_ref}/fail"
    result = await mcp_a.call({"action": "invoke", "id": tool_id, "args": {"message": "nope-42"}})
    error = error_payload(result)
    assert error["code"] == "UPSTREAM_ERROR"
    assert "nope-42" in error["message"]
    assert error["service"] == gateway.data.service_a_ref
    assert error["retryable"] is False


async def test_invoke_via_sse_transport(mcp_b: McpConnection, gateway: GatewayEnv) -> None:
    tool_id = f"{gateway.data.service_b_ref}/echo"
    result = await mcp_b.call({"action": "invoke", "id": tool_id, "args": {"text": "via-sse"}})
    assert result["isError"] is False
    assert make_result_text(result) == "via-sse"


async def test_missing_action_invalid_args(mcp_a: McpConnection) -> None:
    result = await mcp_a.call({})
    error = error_payload(result)
    assert error["code"] == "INVALID_ARGS"
    assert "action" in error["message"]


async def test_unknown_action_invalid_args(mcp_a: McpConnection) -> None:
    result = await mcp_a.call({"action": "nuke"})
    error = error_payload(result)
    assert error["code"] == "INVALID_ARGS"
    assert "nuke" in error["message"]


async def test_skills_actions_validate_arguments(mcp_a: McpConnection) -> None:
    result = await mcp_a.call({"action": "list_skills"})
    payload = make_result_payload(result)
    assert isinstance(payload["skills"], list)
    for action in ("load_skill", "read_skill_file"):
        result = await mcp_a.call({"action": action})
        error = error_payload(result)
        assert error["code"] == "INVALID_ARGS"
        assert "id" in error["message"]
    missing_path = error_payload(
        await mcp_a.call({"action": "read_skill_file", "id": "global/nope"})
    )
    assert missing_path["code"] == "INVALID_ARGS"


async def test_session_bound_to_initializing_token(gateway: GatewayEnv) -> None:
    first = McpConnection(gateway.client, gateway.data.token_a)
    await first.initialize()
    second = McpConnection(gateway.client, gateway.data.token_b)
    second.session_id = first.session_id
    response = await second.post({"jsonrpc": "2.0", "id": 50, "method": "tools/list", "params": {}})
    assert response.status_code == 404


async def test_request_without_session_id_rejected(gateway: GatewayEnv) -> None:
    connection = McpConnection(gateway.client, gateway.data.token_a)
    response = await connection.post(
        {"jsonrpc": "2.0", "id": 60, "method": "tools/list", "params": {}}
    )
    assert response.status_code == 400


async def test_done_when_full_agent_flow(gateway: GatewayEnv) -> None:
    connection = McpConnection(gateway.client, gateway.data.token_a)
    init = await connection.initialize()
    assert init["result"]["instructions"] == SERVER_INSTRUCTIONS

    listed = await connection.result("tools/list")
    cam = next(tool for tool in listed["tools"] if tool["name"] == "cam")
    assert set(cam["inputSchema"]["properties"]["action"]["enum"]) == EXPECTED_ACTIONS

    services_result = await connection.call({"action": "list_services"})
    assert services_result["isError"] is False
    services = services_result["structuredContent"]["services"]
    assert any(s["id"] == gateway.data.service_a_ref for s in services)

    tools_result = await connection.call(
        {"action": "list_tools", "service": gateway.data.service_a_ref}
    )
    assert tools_result["isError"] is False

    described = await connection.call({"action": "describe", "id": gateway.data.echo_tool_id})
    assert described["isError"] is False

    invoked = await connection.call(
        {"action": "invoke", "id": gateway.data.echo_tool_id, "args": {"text": "flow-ok"}}
    )
    assert invoked["isError"] is False
    assert make_result_text(invoked) == "flow-ok"
