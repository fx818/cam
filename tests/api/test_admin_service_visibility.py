"""Admin service visibility routes (`/api/admin/services/{ref}/visibility`)."""

from __future__ import annotations

import uuid
from typing import Any, cast

import httpx

from api.helpers import ApiEnv, auth_headers, post_json
from cam.db.models import User

Response = httpx.Response


async def _create_service(
    api: ApiEnv, admin_token: str, tier_kind: dict[str, object] | None = None
) -> dict[str, Any]:
    slug = f"vis-{uuid.uuid4().hex[:8]}"
    body: dict[str, object] = {
        "slug": slug,
        "name": f"Service {slug}",
        "url": "https://mcp.example.com/mcp",
        "transport": "streamable-http",
    }
    if tier_kind:
        body.update(tier_kind)
    create = await post_json(api.client, "/api/admin/services", body, token=admin_token)
    assert create.status_code == 200, create.text
    return cast(dict[str, Any], create.json()["service"])


async def _patch_visibility(api: ApiEnv, admin_token: str, ref: str, visibility: str) -> Response:
    return await api.client.request(
        "PATCH",
        f"/api/admin/services/{ref}/visibility",
        json={"visibility": visibility},
        headers=await auth_headers(api.client, admin_token),
    )


async def _grant(
    api: ApiEnv, admin_token: str, ref: str, email: str, method: str = "POST"
) -> Response:
    return await api.client.request(
        method,
        f"/api/admin/services/{ref}/visibility/grant",
        json={"email": email},
        headers=await auth_headers(api.client, admin_token),
    )


async def test_admin_sets_visibility_restricted(api: ApiEnv, admin_token: str) -> None:
    service = await _create_service(api, admin_token)
    patch = await _patch_visibility(api, admin_token, service["id"], "restricted")
    assert patch.status_code == 200, patch.text
    state = patch.json()
    assert state["visibility"] == "restricted"
    assert state["grantees"] == []
    back = await _patch_visibility(api, admin_token, service["id"], "all")
    assert back.status_code == 200
    assert back.json()["visibility"] == "all"


async def test_grant_and_revoke_visibility_by_email(
    api: ApiEnv, admin_token: str, api_user: User
) -> None:
    service = await _create_service(api, admin_token)
    await _patch_visibility(api, admin_token, service["id"], "restricted")
    grant = await _grant(api, admin_token, service["id"], api_user.email)
    assert grant.status_code == 200, grant.text
    state = grant.json()
    assert state["visibility"] == "restricted"
    assert [grantee["email"] for grantee in state["grantees"]] == [api_user.email]
    duplicate = await _grant(api, admin_token, service["id"], api_user.email)
    assert duplicate.status_code == 409
    revoke = await _grant(api, admin_token, service["id"], api_user.email, method="DELETE")
    assert revoke.status_code == 200, revoke.text
    assert revoke.json()["grantees"] == []
    missing = await _grant(api, admin_token, service["id"], api_user.email, method="DELETE")
    assert missing.status_code == 404


async def test_revoke_endpoint_requires_existing_grant(api: ApiEnv, admin_token: str) -> None:
    service = await _create_service(api, admin_token)
    await _patch_visibility(api, admin_token, service["id"], "restricted")
    revoke = await api.client.request(
        "POST",
        f"/api/admin/services/{service['id']}/visibility/revoke",
        json={"email": "nobody-when@example.com"},
        headers=await auth_headers(api.client, admin_token),
    )
    assert revoke.status_code == 404


async def test_non_admin_cannot_change_visibility(api: ApiEnv, user_token: str) -> None:
    response = await api.client.request(
        "PATCH",
        "/api/admin/services/global/whatever/visibility",
        json={"visibility": "restricted"},
        headers=await auth_headers(api.client, user_token),
    )
    assert response.status_code == 403


async def test_unknown_service_visibility_is_404(api: ApiEnv, admin_token: str) -> None:
    response = await _patch_visibility(api, admin_token, "global/no-such-service", "restricted")
    assert response.status_code == 404


async def test_restricted_service_hidden_from_ungranted_user(
    api: ApiEnv, admin_token: str, user_token: str, api_user: User
) -> None:
    service = await _create_service(api, admin_token)
    ref = service["id"]
    mine = await api.client.get("/api/services", headers=await auth_headers(api.client, user_token))
    assert mine.status_code == 200
    assert ref in [item["id"] for item in mine.json()["services"]]
    patch = await _patch_visibility(api, admin_token, ref, "restricted")
    assert patch.status_code == 200
    hidden = await api.client.get(
        "/api/services", headers=await auth_headers(api.client, user_token)
    )
    assert ref not in [item["id"] for item in hidden.json()["services"]]
    grant = await _grant(api, admin_token, ref, api_user.email)
    assert grant.status_code == 200
    shared = await api.client.get(
        "/api/services", headers=await auth_headers(api.client, user_token)
    )
    assert ref in [item["id"] for item in shared.json()["services"]]
