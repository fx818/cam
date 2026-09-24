from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import UserEnv, login, mutate_json, post_json
from cam.connections.url_policy import load_url_allowlist
from cam.db.models import Org, Service, UrlAllowlist, User


@pytest.fixture(autouse=True)
async def restore_shared_org(db_session: AsyncSession) -> AsyncGenerator[None]:
    yield
    await db_session.execute(update(Org).where(Org.slug == "indiamart").values(settings={}))
    await db_session.execute(delete(UrlAllowlist))
    await db_session.commit()


async def test_settings_require_admin(env_user: UserEnv, user_password: str) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    response = await env_user.client.get("/api/admin/settings")
    assert response.status_code == 403


async def test_get_settings_defaults(admin_env: UserEnv, user_password: str) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await admin_env.client.get("/api/admin/settings")
    assert response.status_code == 200
    settings = response.json()["settings"]
    assert settings["default_timeout_ms"] == 60000
    assert settings["response_cap_tokens"] == 50000
    assert settings["log_retention_days"] == 30


async def test_patch_settings_roundtrip(admin_env: UserEnv, user_password: str) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await mutate_json(
        admin_env.client,
        "PATCH",
        "/api/admin/settings",
        {
            "default_timeout_ms": 45000,
            "response_cap_tokens": 25000,
            "log_retention_days": 14,
            "allowed_email_domains": ["Example.COM", "indiamart.com"],
        },
    )
    assert response.status_code == 200
    settings = response.json()["settings"]
    assert settings["default_timeout_ms"] == 45000
    assert settings["log_retention_days"] == 14
    assert settings["allowed_email_domains"] == ["example.com", "indiamart.com"]

    again = await admin_env.client.get("/api/admin/settings")
    assert again.json()["settings"]["default_timeout_ms"] == 45000


async def test_patch_settings_rejects_bad_values(admin_env: UserEnv, user_password: str) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await mutate_json(
        admin_env.client,
        "PATCH",
        "/api/admin/settings",
        {"log_retention_days": 0},
    )
    assert response.status_code == 422


async def test_allowlist_requires_admin(env_user: UserEnv, user_password: str) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    response = await env_user.client.get("/api/admin/url-allowlist")
    assert response.status_code == 403


async def test_allowlist_add_list_delete(
    admin_env: UserEnv,
    user_password: str,
    db_session: AsyncSession,
    org: Org,
) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    created = await post_json(
        admin_env.client,
        "/api/admin/url-allowlist",
        {"pattern": "10.1.0.0/16", "note": "staging range"},
    )
    assert created.status_code == 201
    entry = created.json()["entry"]
    assert entry["pattern"] == "10.1.0.0/16"
    assert entry["note"] == "staging range"
    assert entry["created_by_email"] == admin_env.user.email

    listed = await admin_env.client.get("/api/admin/url-allowlist")
    assert listed.status_code == 200
    patterns = [e["pattern"] for e in listed.json()["entries"]]
    assert "10.1.0.0/16" in patterns

    allowed = await load_url_allowlist(db_session, org_id=org.id)
    assert "10.1.0.0/16" in allowed

    deleted = await mutate_json(
        admin_env.client, "DELETE", f"/api/admin/url-allowlist/{entry['id']}"
    )
    assert deleted.status_code == 200

    listed_again = await admin_env.client.get("/api/admin/url-allowlist")
    assert "10.1.0.0/16" not in [e["pattern"] for e in listed_again.json()["entries"]]


async def test_allowlist_rejects_duplicates(admin_env: UserEnv, user_password: str) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    body: dict[str, object] = {"pattern": "intranet.example.com"}
    first = await post_json(admin_env.client, "/api/admin/url-allowlist", body)
    assert first.status_code == 201
    second = await post_json(admin_env.client, "/api/admin/url-allowlist", body)
    assert second.status_code == 422
    assert "already allowlisted" in second.json()["error"]["message"]


async def test_allowlist_rejects_bad_pattern(admin_env: UserEnv, user_password: str) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await post_json(
        admin_env.client, "/api/admin/url-allowlist", {"pattern": "not a host!"}
    )
    assert response.status_code == 422


async def test_allowlist_delete_unknown_404(admin_env: UserEnv, user_password: str) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await mutate_json(
        admin_env.client, "DELETE", f"/api/admin/url-allowlist/{uuid.uuid4()}"
    )
    assert response.status_code == 404


async def test_overview_returns_stats_and_recent_audit(
    admin_env: UserEnv,
    user_password: str,
    global_service: tuple[Service, object],
    api_user: User,
) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await admin_env.client.get("/api/admin/overview")
    assert response.status_code == 200
    payload = response.json()
    assert payload["users"]["total"] >= 2
    assert payload["users"]["active"] >= 2
    assert payload["services"]["by_tier"]["global"] >= 1
    assert isinstance(payload["recent_audit"], list)
