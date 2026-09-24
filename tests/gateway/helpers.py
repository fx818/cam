"""Shared helpers for gateway tests (imported as gateway.helpers)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

import httpx
from fastapi import FastAPI


@dataclass(frozen=True, slots=True)
class GatewayData:
    token_a: str
    token_b: str
    token_revoked: str
    service_a_ref: str
    service_b_ref: str
    service_a_slug: str
    service_b_slug: str
    echo_tool_id: str
    disabled_tool_id: str


@dataclass(slots=True)
class GatewayEnv:
    app: FastAPI
    client: httpx.AsyncClient
    data: GatewayData


def _as_object(payload: Any, *, context: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AssertionError(f"{context}: expected a JSON object, got {payload!r}")
    return cast("dict[str, Any]", payload)


def text_blocks(result: dict[str, Any]) -> list[dict[str, Any]]:
    content = result.get("content")
    if not isinstance(content, list):
        return []
    items = cast("list[Any]", content)
    blocks: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            blocks.append(cast("dict[str, Any]", item))
    return blocks


def make_result_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in text_blocks(result):
        if block.get("type") != "text":
            continue
        text = block.get("text", "")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def make_result_payload(result: dict[str, Any]) -> dict[str, Any]:
    return _as_object(json.loads(make_result_text(result)), context="tool result payload")


def error_payload(result: dict[str, Any]) -> dict[str, Any]:
    assert result.get("isError") is True, result
    wrapper = make_result_payload(result)
    error = wrapper.get("error")
    if not isinstance(error, dict):
        raise AssertionError(f"missing error envelope in {wrapper!r}")
    err = cast("dict[str, Any]", error)
    for key in ("code", "message", "service", "retryable"):
        assert key in err, err
    assert len(err) == 4, err
    return err


def parse_sse_message(response: httpx.Response) -> dict[str, Any]:
    data_lines = [
        line.removeprefix("data:").lstrip()
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]
    assert data_lines, f"response had no SSE data: {response.text!r}"
    message = json.loads("\n".join(data_lines))
    return _as_object(message, context="SSE message")


class McpConnection:
    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self._client = client
        self._token = token
        self.session_id: str | None = None
        self._next_id = 0

    def headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "authorization": f"Bearer {self._token}",
        }
        if self.session_id is not None:
            headers["mcp-session-id"] = self.session_id
        return headers

    async def post(self, payload: dict[str, Any]) -> httpx.Response:
        return await self._client.post("/mcp", json=payload, headers=self.headers())

    async def initialize(self) -> dict[str, Any]:
        self._next_id += 1
        response = await self.post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "claude-code", "version": "1.0.0"},
                },
            }
        )
        assert response.status_code == 200, response.text
        self.session_id = response.headers.get("mcp-session-id")
        assert self.session_id, dict(response.headers)
        message = parse_sse_message(response)
        await self.notify("notifications/initialized")
        return message

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        response = await self.post(payload)
        assert response.status_code == 202, (response.status_code, response.text)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        response = await self.post(payload)
        assert response.status_code == 200, response.text
        return parse_sse_message(response)

    async def result(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        message = await self.request(method, params)
        assert "error" not in message, message
        result = message.get("result")
        if not isinstance(result, dict):
            raise AssertionError(f"missing result in message: {message!r}")
        return cast("dict[str, Any]", result)

    async def call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.result("tools/call", {"name": "cam", "arguments": arguments})
