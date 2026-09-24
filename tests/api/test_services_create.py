"""Phase 3.4 routes: create service, test connection, define manual tool."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import ApiEnv, post_json
from cam.db.models import Connection, Service, User
from fixtures.mock_mcp import MockMCPHandle
from fixtures.mock_rest import MockRestHandle


def _mcp_body(url: str, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "slug": f"svc-{uuid.uuid4().hex[:8]}",
        "name": "Example MCP",
        "url": url,
        "transport": "streamable-http",
        "auth_type": "none",
        "description": "from test",
        "timeout_ms": 15000,
        "response_cap_tokens": 8192,
    }
    body.update(overrides)
    return body


async def test_create_personal_mcp_service(api: ApiEnv, user_token: str, api_user: User) -> None:
    body = _mcp_body("https://mcp.example.com/mcp")
    response = await post_json(api.client, "/api/services", body, token=user_token)
    assert response.status_code == 200
    payload = response.json()
    service = payload["service"]
    assert service["id"] == f"{api_user.handle}/{body['slug']}"
    assert service["tier"] == "personal"
    assert service["kind"] == "mcp"
    assert service["transport"] == "streamable-http"
    assert service["owner"] == api_user.handle
    assert service["status"] == "active"
    assert "credential" not in service


async def test_create_service_requires_auth(api: ApiEnv) -> None:
    response = await api.client.post("/api/services", json=_mcp_body("https://x.example/mcp"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_create_service_duplicate_slug_conflicts(api: ApiEnv, user_token: str) -> None:
    body = _mcp_body("https://mcp.example.com/mcp")
    first = await post_json(api.client, "/api/services", body, token=user_token)
    assert first.status_code == 200
    second = await post_json(api.client, "/api/services", body, token=user_token)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFLICT"


async def test_create_service_validation_errors(api: ApiEnv, user_token: str) -> None:
    bad_slug = _mcp_body("https://mcp.example.com/mcp", slug="Bad Slug!")
    response = await post_json(api.client, "/api/services", bad_slug, token=user_token)
    assert response.status_code == 422

    missing_transport = _mcp_body("https://mcp.example.com/mcp", transport=None)
    response = await post_json(api.client, "/api/services", missing_transport, token=user_token)
    assert response.status_code == 422

    bad_auth = _mcp_body("https://mcp.example.com/mcp", auth_type="bearer", credential=None)
    response = await post_json(api.client, "/api/services", bad_auth, token=user_token)
    assert response.status_code == 422


async def test_create_service_stores_credential_encrypted(
    api: ApiEnv, user_token: str, db_session: AsyncSession, api_user: User
) -> None:
    body = _mcp_body(
        "https://mcp.example.com/mcp",
        auth_type="bearer",
        credential="super-secret-token",
    )
    response = await post_json(api.client, "/api/services", body, token=user_token)
    assert response.status_code == 200
    service_id = response.json()["service"]["id"]
    slug = service_id.split("/", 1)[1]

    row = (
        await db_session.execute(
            select(Service).where(Service.slug == slug, Service.owner_user_id == api_user.id)
        )
    ).scalar_one()
    connection = (
        await db_session.execute(select(Connection).where(Connection.service_id == row.id))
    ).scalar_one()
    assert connection.secret_enc is not None
    assert b"super-secret-token" not in connection.secret_enc
    assert "super-secret-token" not in response.text


async def test_test_endpoint_rest_lists_tools(
    api: ApiEnv, user_token: str, mock_rest: MockRestHandle, loopback_allowlist: None
) -> None:
    body = {
        "slug": f"rest-{uuid.uuid4().hex[:8]}",
        "name": "Mock REST",
        "url": mock_rest.base_url,
        "kind": "rest",
        "auth_type": "none",
        "spec_url": mock_rest.spec_url,
    }
    response = await post_json(api.client, "/api/services/test", body, token=user_token)
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    names = {tool["name"] for tool in payload["tools"]}
    assert {"listItems", "getItem", "createItem"} <= names


async def test_test_endpoint_rest_spec_failure_is_200(
    api: ApiEnv, user_token: str, loopback_allowlist: None
) -> None:
    body = {
        "slug": f"rest-{uuid.uuid4().hex[:8]}",
        "name": "Broken REST",
        "url": "http://127.0.0.1:9",
        "kind": "rest",
        "auth_type": "none",
        "spec_url": "http://127.0.0.1:9/openapi.json",
    }
    response = await post_json(api.client, "/api/services/test", body, token=user_token)
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["tools"] == []
    assert payload["message"]


async def test_test_endpoint_mcp_lists_tools(
    api: ApiEnv, user_token: str, mock_mcp: MockMCPHandle, loopback_allowlist: None
) -> None:
    body = _mcp_body(mock_mcp.streamable_url)
    response = await post_json(api.client, "/api/services/test", body, token=user_token)
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert len(payload["tools"]) > 0
    assert all("name" in tool for tool in payload["tools"])


async def test_test_endpoint_mcp_unreachable_is_200(
    api: ApiEnv, user_token: str, loopback_allowlist: None
) -> None:
    body = _mcp_body("http://127.0.0.1:9/mcp")
    response = await post_json(api.client, "/api/services/test", body, token=user_token)
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["tools"] == []
    assert payload["message"]


async def test_create_rest_service_imports_tools(
    api: ApiEnv, user_token: str, mock_rest: MockRestHandle, loopback_allowlist: None
) -> None:
    body = {
        "slug": f"rest-{uuid.uuid4().hex[:8]}",
        "name": "Mock REST Service",
        "url": mock_rest.base_url,
        "kind": "rest",
        "transport": None,
        "auth_type": "none",
        "spec_url": mock_rest.spec_url,
        "tools": ["listItems", "getItem"],
    }
    response = await post_json(api.client, "/api/services", body, token=user_token)
    assert response.status_code == 200
    service = response.json()["service"]
    assert service["kind"] == "rest"
    assert service["spec_url"] == mock_rest.spec_url


async def test_define_manual_tool_on_rest_service(
    api: ApiEnv, user_token: str, mock_rest: MockRestHandle, loopback_allowlist: None
) -> None:
    create = await post_json(
        api.client,
        "/api/services",
        {
            "slug": f"manual-{uuid.uuid4().hex[:8]}",
            "name": "Manual REST",
            "url": mock_rest.base_url,
            "kind": "rest",
            "auth_type": "none",
        },
        token=user_token,
    )
    assert create.status_code == 200
    ref = create.json()["service"]["id"]
    owner, slug = ref.split("/", 1)

    response = await post_json(
        api.client,
        f"/api/services/{owner}/{slug}/tools",
        {
            "name": "getItem",
            "method": "GET",
            "path": "/items/{item_id}",
            "params": {"item_id": "path"},
            "description": "Fetch an item",
        },
        token=user_token,
    )
    assert response.status_code == 200
    tool = response.json()["tool"]
    assert tool["name"] == "getItem"
    assert tool["enabled"] is True


async def test_define_manual_tool_rejects_mcp_service(api: ApiEnv, user_token: str) -> None:
    create = await post_json(
        api.client,
        "/api/services",
        _mcp_body("https://mcp.example.com/mcp"),
        token=user_token,
    )
    assert create.status_code == 200
    ref = create.json()["service"]["id"]
    owner, slug = ref.split("/", 1)
    response = await post_json(
        api.client,
        f"/api/services/{owner}/{slug}/tools",
        {"name": "t", "method": "GET", "path": "/x"},
        token=user_token,
    )
    assert response.status_code == 422


async def test_define_manual_tool_hidden_from_non_owner(
    api: ApiEnv, user_token: str, db_session: AsyncSession
) -> None:
    from cam.accounts.users import create_user
    from cam.connections.tokens import create_token

    suffix = uuid.uuid4().hex[:10]
    other = await create_user(
        db_session,
        email=f"other-{suffix}@example.com",
        password=f"pw-{suffix}",
        role="user",
    )
    other_token, _ = await create_token(
        db_session, org_id=other.org_id, user_id=other.id, label="other"
    )
    await db_session.commit()

    create = await post_json(
        api.client,
        "/api/services",
        {
            "slug": f"priv-{uuid.uuid4().hex[:8]}",
            "name": "Private",
            "url": "https://mcp.example.com/mcp",
            "transport": "streamable-http",
            "auth_type": "none",
        },
        token=user_token,
    )
    assert create.status_code == 200
    ref = create.json()["service"]["id"]
    owner, slug = ref.split("/", 1)

    response = await post_json(
        api.client,
        f"/api/services/{owner}/{slug}/tools",
        {"name": "t", "method": "GET", "path": "/x"},
        token=other_token,
    )
    assert response.status_code == 404


async def test_tools_endpoint_unknown_service_is_404(api: ApiEnv, user_token: str) -> None:
    response = await post_json(
        api.client,
        "/api/services/global/nope/tools",
        {"name": "t", "method": "GET", "path": "/x"},
        token=user_token,
    )
    assert response.status_code == 404
