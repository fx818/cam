"""Admin template registration route (`POST /api/admin/services`)."""

from __future__ import annotations

import uuid

from api.helpers import ApiEnv, post_json


async def test_admin_can_create_template(api: ApiEnv, admin_token: str) -> None:
    slug = f"tpl-{uuid.uuid4().hex[:8]}"
    create = await post_json(
        api.client,
        "/api/admin/services",
        {
            "slug": slug,
            "name": "Notion Template",
            "url": "https://api.notion.com/v1",
            "kind": "rest",
            "auth_type": "bearer",
            "credential": "org-fallback",
            "description": "Shared Notion access",
        },
        token=admin_token,
    )
    assert create.status_code == 200
    service = create.json()["service"]
    assert service["tier"] == "template"
    assert service["id"] == f"global/{slug}"


async def test_non_admin_cannot_create_template(api: ApiEnv, user_token: str) -> None:
    response = await post_json(
        api.client,
        "/api/admin/services",
        {
            "slug": f"nope-{uuid.uuid4().hex[:8]}",
            "name": "Nope",
            "url": "https://api.example.com",
        },
        token=user_token,
    )
    assert response.status_code == 403


async def test_template_slug_conflict_is_409(api: ApiEnv, admin_token: str) -> None:
    slug = f"conflict-{uuid.uuid4().hex[:8]}"
    first = await post_json(
        api.client,
        "/api/admin/services",
        {"slug": slug, "name": "One", "url": "https://a.example.com", "kind": "rest"},
        token=admin_token,
    )
    second = await post_json(
        api.client,
        "/api/admin/services",
        {
            "slug": slug,
            "name": "Two",
            "url": "https://b.example.com",
            "kind": "rest",
        },
        token=admin_token,
    )
    assert first.status_code == 200
    assert second.status_code == 409


async def test_template_requires_valid_slug(api: ApiEnv, admin_token: str) -> None:
    response = await post_json(
        api.client,
        "/api/admin/services",
        {"slug": "Bad Slug!", "name": "Bad", "url": "https://b.example.com"},
        token=admin_token,
    )
    assert response.status_code == 422
