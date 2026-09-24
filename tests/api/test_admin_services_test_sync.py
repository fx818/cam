"""Admin register-with-test and row-level Test/Sync routes."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import ApiEnv, auth_headers, ensure_csrf, post_json
from cam.db.models import Service, Tool
from fixtures.mock_mcp import MockMCPHandle


@pytest.fixture
async def admin_headers(api: ApiEnv, admin_token: str) -> dict[str, str]:
    return await auth_headers(api.client, admin_token)


def _admin_body(url: str, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "slug": f"g-{uuid.uuid4().hex[:8]}",
        "name": "Global Mock",
        "url": url,
        "transport": "streamable-http",
        "auth_type": "none",
        "tier": "global",
    }
    body.update(overrides)
    return body


async def test_register_global_with_test_imports_pending_tools(
    api: ApiEnv,
    admin_token: str,
    mock_mcp: MockMCPHandle,
    loopback_allowlist: None,
    db_session: AsyncSession,
) -> None:
    body = _admin_body(mock_mcp.streamable_url)
    response = await post_json(api.client, "/api/admin/services", body, token=admin_token)
    assert response.status_code == 200
    payload = response.json()
    test = payload["test"]
    assert test["ok"] is True
    assert test["tool_count"] > 0
    assert test["imported"] == test["tool_count"]
    assert test["pending_review_count"] == test["tool_count"]
    assert test["error"] is None
    ref = payload["service"]["id"]
    slug = ref.split("/", 1)[1]
    result = await db_session.execute(
        select(Tool.status).join(Service, Service.id == Tool.service_id).where(Service.slug == slug)
    )
    statuses = result.scalars().all()
    assert statuses
    assert all(status == "pending_review" for status in statuses)
    assert "credential" not in payload["service"]["auth_config"]


async def test_register_without_test_skips_test_block(
    api: ApiEnv, admin_token: str, mock_mcp: MockMCPHandle, loopback_allowlist: None
) -> None:
    body = _admin_body(mock_mcp.streamable_url, test=False)
    response = await post_json(api.client, "/api/admin/services", body, token=admin_token)
    assert response.status_code == 200
    assert "test" not in response.json()


async def test_register_wrong_credential_returns_auth_hint(
    api: ApiEnv, admin_token: str, mock_mcp_authed: MockMCPHandle, loopback_allowlist: None
) -> None:
    body = _admin_body(
        mock_mcp_authed.streamable_url,
        auth_type="static-header",
        header_name="Authorization",
        credential="test-secret",
    )
    response = await post_json(api.client, "/api/admin/services", body, token=admin_token)
    assert response.status_code == 200
    payload = response.json()
    assert payload["service"]["status"] == "active"
    test = payload["test"]
    assert test["ok"] is False
    assert test["tool_count"] == 0
    assert test["error"]
    assert "Bearer " in test["hint"]
    assert "test-secret" not in response.text


async def test_register_failure_of_test_keeps_service(
    api: ApiEnv, admin_token: str, mock_mcp_authed: MockMCPHandle, loopback_allowlist: None
) -> None:
    body = _admin_body(
        mock_mcp_authed.streamable_url,
        auth_type="bearer",
        credential="wrong",
    )
    response = await post_json(api.client, "/api/admin/services", body, token=admin_token)
    assert response.status_code == 200
    assert response.json()["service"]["tier"] == "global"


async def test_admin_services_list_includes_tool_counts(
    api: ApiEnv,
    admin_headers: dict[str, str],
    admin_token: str,
    mock_mcp: MockMCPHandle,
    loopback_allowlist: None,
) -> None:
    slug = f"g-{uuid.uuid4().hex[:8]}"
    create = await post_json(
        api.client,
        "/api/admin/services",
        _admin_body(mock_mcp.streamable_url, slug=slug),
        token=admin_token,
    )
    assert create.status_code == 200
    listing = await api.client.get("/api/admin/services", headers=admin_headers)
    assert listing.status_code == 200
    match = [s for s in listing.json()["services"] if s["slug"] == slug]
    assert match
    counts = match[0]["tool_counts"]
    assert counts.get("pending_review", 0) > 0
    assert "removed_upstream" not in counts


async def test_admin_resync_route_happy_path(
    api: ApiEnv,
    admin_headers: dict[str, str],
    admin_token: str,
    mock_mcp: MockMCPHandle,
    loopback_allowlist: None,
) -> None:
    slug = f"g-{uuid.uuid4().hex[:8]}"
    mock_mcp.add_tool(f"extra-{slug}")
    body = _admin_body(mock_mcp.streamable_url, slug=slug, test=False)
    create = await post_json(api.client, "/api/admin/services", body, token=admin_token)
    assert create.status_code == 200
    response = await api.client.post(
        f"/api/admin/services/global/{slug}/resync",
        headers=admin_headers,
    )
    assert response.status_code == 200
    payload = response.json()
    test = payload["test"]
    assert payload["service"] == f"global/{slug}"
    assert test["ok"] is True
    assert test["tool_count"] > 0
    assert test["pending_review_count"] > 0
    assert test["error"] is None


async def test_admin_resync_route_unknown_service_is_404(
    api: ApiEnv, admin_headers: dict[str, str]
) -> None:
    response = await api.client.post(
        "/api/admin/services/global/nope/resync",
        headers=admin_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_admin_resync_route_requires_auth(api: ApiEnv) -> None:
    csrf = await ensure_csrf(api.client)
    response = await api.client.post(
        "/api/admin/services/global/nope/resync",
        json={},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 401
