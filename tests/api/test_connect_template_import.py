"""Templates: the first usable connection triggers a tool import.

A template is registered without a credential, so its first import can only
happen once a user connects. Connecting enqueues a coordinated re-sync, which
imports tools as `pending_review`; an admin approves them in the review queue
and the tool becomes available to the user.
"""

from __future__ import annotations

import asyncio
import uuid

from api.helpers import ApiEnv, auth_headers, post_json
from fixtures.mock_mcp import MockMCPHandle


async def test_template_first_connect_imports_then_review_approves(
    api: ApiEnv,
    admin_token: str,
    user_token: str,
    mock_mcp_authed: MockMCPHandle,
    loopback_allowlist: None,
) -> None:
    admin_headers = await auth_headers(api.client, admin_token)
    slug = f"tpl-{uuid.uuid4().hex[:8]}"
    create = await post_json(
        api.client,
        "/api/admin/services",
        {
            "slug": slug,
            "name": "Gke MCP",
            "url": mock_mcp_authed.streamable_url,
            "transport": "streamable-http",
            "auth_type": "bearer",
            "test": False,
        },
        token=admin_token,
    )
    assert create.status_code == 200
    ref = create.json()["service"]["id"]
    assert ref == f"global/{slug}"

    listing = await api.client.get("/api/admin/services", headers=admin_headers)
    row = next(row for row in listing.json()["services"] if row["slug"] == slug)
    assert row["tool_counts"].get("active", 0) + row["tool_counts"].get("pending_review", 0) == 0

    connect = await post_json(
        api.client,
        f"/api/services/{ref}/connect",
        {"credential": "test-secret"},
        token=user_token,
    )
    assert connect.status_code == 200
    assert connect.json()["connection"]["status"] == "connected"

    pending_ids: list[str] = []
    for _ in range(60):
        queue = await api.client.get("/api/admin/review-queue", headers=admin_headers)
        assert queue.status_code == 200
        for entry in queue.json()["tools"]:
            if entry["service_ref"] == f"global/{slug}":
                pending_ids.append(entry["id"])
        if pending_ids:
            break
        await asyncio.sleep(0.25)
    assert pending_ids, "connect did not trigger a template tool import"

    approve = await post_json(
        api.client,
        "/api/admin/review-queue/approve",
        {"tool_ids": pending_ids},
        token=admin_token,
    )
    assert approve.status_code == 200
    assert sorted(approve.json()["approved"]) == sorted(pending_ids)
    assert approve.json()["unknown"] == []

    tools = await api.client.get(
        f"/api/services/{ref}/tools", headers=await auth_headers(api.client, user_token)
    )
    assert tools.status_code == 200
    payload = tools.json()
    by_name = {tool["name"]: tool for tool in payload["tools"]}
    assert "echo" in by_name
    for tool in payload["tools"]:
        assert tool["status"] == "active"
        assert tool["enabled"] is True

    recheck = await api.client.get("/api/admin/services", headers=admin_headers)
    row = next(row for row in recheck.json()["services"] if row["slug"] == slug)
    assert row["tool_counts"].get("pending_review", 0) == 0
    assert row["tool_counts"].get("active", 0) > 0
