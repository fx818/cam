"""Phase 3.1 e2e: invoke REST tools through /mcp against the mock REST API."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from cam.api.main import create_app
from cam.config import Settings
from cam.connections.orgs import encrypt_for_org, ensure_org
from cam.connections.tokens import create_token
from cam.db.models import Connection, Service, Tool, User
from cam.db.session import create_db_engine, create_session_factory
from fixtures.mock_rest import API_KEY_NAME, API_KEY_VALUE, MockRestHandle
from gateway.helpers import McpConnection, error_payload, make_result_payload, make_result_text


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

    task = asyncio.create_task(_run_lifespan(), name="cam-rest-lifespan")
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


def _rest_binding(
    method: str, path: str, params: dict[str, str] | None = None
) -> dict[str, object]:
    return {"method": method, "path": path, "params": params or {}}


async def _seed_rest_service(
    mock_rest: MockRestHandle, *, slug_suffix: str
) -> tuple[str, str, str]:
    engine = create_db_engine(Settings().database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            org = await ensure_org(session)
            user = User(
                org_id=org.id,
                email=f"rest-{slug_suffix}@example.com",
                handle=f"rest{slug_suffix}",
                display_name="REST Tester",
                password_hash="x",
                role="user",
                kind="person",
            )
            session.add(user)
            await session.flush()
            raw_token, _ = await create_token(
                session, org_id=org.id, user_id=user.id, label="rest-invoker"
            )
            service = Service(
                org_id=org.id,
                tier="global",
                owner_user_id=None,
                slug=f"mockrest{slug_suffix}",
                name="Mock REST",
                description="Mock REST API for phase 3.1",
                kind="rest",
                url=mock_rest.base_url,
                transport=None,
                auth_type="api-key",
                auth_config={"api_key_name": API_KEY_NAME, "api_key_in": "header"},
                visibility="all",
                status="active",
                health="unknown",
            )
            session.add(service)
            await session.flush()
            secret_enc, key_version = await encrypt_for_org(session, org.id, API_KEY_VALUE)
            session.add(
                Connection(
                    org_id=org.id,
                    service_id=service.id,
                    user_id=None,
                    secret_enc=secret_enc,
                    key_version=key_version,
                    status="connected",
                )
            )
            session.add_all(
                [
                    Tool(
                        org_id=org.id,
                        service_id=service.id,
                        name="list_items",
                        upstream_name="listItems",
                        description="List items",
                        input_schema={
                            "type": "object",
                            "properties": {"q": {"type": "string"}},
                        },
                        rest_binding=_rest_binding("GET", "/items", {"q": "query"}),
                        enabled=True,
                        status="active",
                    ),
                    Tool(
                        org_id=org.id,
                        service_id=service.id,
                        name="get_item",
                        upstream_name="getItem",
                        description="Get one item",
                        input_schema={
                            "type": "object",
                            "properties": {"item_id": {"type": "string"}},
                            "required": ["item_id"],
                        },
                        rest_binding=_rest_binding("GET", "/items/{item_id}", {"item_id": "path"}),
                        enabled=True,
                        status="active",
                    ),
                    Tool(
                        org_id=org.id,
                        service_id=service.id,
                        name="missing",
                        upstream_name="missing",
                        description="Always 404",
                        input_schema={"type": "object", "properties": {}},
                        rest_binding=_rest_binding("GET", "/missing"),
                        enabled=True,
                        status="active",
                    ),
                    Tool(
                        org_id=org.id,
                        service_id=service.id,
                        name="boom",
                        upstream_name="boom",
                        description="Always 500",
                        input_schema={"type": "object", "properties": {}},
                        rest_binding=_rest_binding("GET", "/boom"),
                        enabled=True,
                        status="active",
                    ),
                    Tool(
                        org_id=org.id,
                        service_id=service.id,
                        name="throttled",
                        upstream_name="throttled",
                        description="Always 429",
                        input_schema={"type": "object", "properties": {}},
                        rest_binding=_rest_binding("GET", "/throttled"),
                        enabled=True,
                        status="active",
                    ),
                    Tool(
                        org_id=org.id,
                        service_id=service.id,
                        name="plain",
                        upstream_name="plainText",
                        description="Plain text",
                        input_schema={"type": "object", "properties": {}},
                        rest_binding=_rest_binding("GET", "/plain"),
                        enabled=True,
                        status="active",
                    ),
                ]
            )
            await session.commit()
            ref = f"global/{service.slug}"
            return raw_token, ref, user.email
    finally:
        await engine.dispose()


async def test_rest_invoke_end_to_end(mock_rest: MockRestHandle) -> None:
    suffix = uuid.uuid4().hex[:8]
    token, ref, _email = await _seed_rest_service(mock_rest, slug_suffix=suffix)
    mock_rest.requests.clear()
    app = create_app(Settings())
    async with _running_app(app) as client:
        connection = McpConnection(client, token)
        await connection.initialize()

        listed = await connection.call({"action": "list_services"})
        assert listed["isError"] is False
        ids = [service["id"] for service in make_result_payload(listed)["services"]]
        assert ref in ids

        tools = await connection.call({"action": "list_tools", "service": ref})
        tool_ids = {tool["id"] for tool in make_result_payload(tools)["tools"]}
        assert f"{ref}/list_items" in tool_ids

        described = await connection.call({"action": "describe", "id": f"{ref}/list_items"})
        payload = make_result_payload(described)
        assert "q" in payload["input_schema"]["properties"]

        invoked = await connection.call(
            {"action": "invoke", "id": f"{ref}/list_items", "args": {"q": "beta"}}
        )
        assert invoked["isError"] is False, invoked
        rows = json.loads(make_result_text(invoked))
        assert [row["name"] for row in rows] == ["beta"]
        seen = [request for request in mock_rest.requests if request.path == "/items"]
        assert seen and seen[-1].headers[API_KEY_NAME.lower()] == API_KEY_VALUE

        detail = await connection.call(
            {"action": "invoke", "id": f"{ref}/get_item", "args": {"item_id": "i-2"}}
        )
        assert detail["isError"] is False
        assert json.loads(make_result_text(detail))["name"] == "beta"

        text = await connection.call({"action": "invoke", "id": f"{ref}/plain", "args": {}})
        assert make_result_text(text) == "hello from mock rest"

        not_found = await connection.call({"action": "invoke", "id": f"{ref}/missing", "args": {}})
        error = error_payload(not_found)
        assert error["code"] == "UPSTREAM_ERROR"
        assert "no such thing" in error["message"]
        assert error["service"] == ref
        assert error["retryable"] is False

        unavailable = await connection.call({"action": "invoke", "id": f"{ref}/boom", "args": {}})
        error = error_payload(unavailable)
        assert error["code"] == "UPSTREAM_UNAVAILABLE"
        assert "Mock REST is unavailable or timed out" in error["message"]
        assert error["retryable"] is True

        rate = await connection.call({"action": "invoke", "id": f"{ref}/throttled", "args": {}})
        error = error_payload(rate)
        assert error["code"] == "UPSTREAM_RATE_LIMITED"
        assert "Rate limited by Mock REST" in error["message"]
        assert error["retryable"] is True

        invalid = await connection.call({"action": "invoke", "id": f"{ref}/get_item", "args": {}})
        error = error_payload(invalid)
        assert error["code"] == "INVALID_ARGS"
        assert "item_id" in error["message"]
