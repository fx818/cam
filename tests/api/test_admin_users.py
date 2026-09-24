from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import ApiEnv, UserEnv, login, mutate_json, post_json
from cam.connections.tokens import create_token
from cam.db.models import CamToken, User


async def test_admin_routes_require_admin(env_user: UserEnv, user_password: str) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    for path in ("/api/admin/users", "/api/admin/settings", "/api/admin/overview"):
        response = await env_user.client.get(path)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_admin_routes_require_session(api: ApiEnv) -> None:
    response = await api.client.get("/api/admin/users")
    assert response.status_code == 401


async def test_list_users(admin_env: UserEnv, user_password: str, api_user: User) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await admin_env.client.get("/api/admin/users")
    assert response.status_code == 200
    emails = [u["email"] for u in response.json()["users"]]
    assert api_user.email in emails
    assert admin_env.user.email in emails


async def test_create_user(admin_env: UserEnv, user_password: str) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await post_json(
        admin_env.client,
        "/api/admin/users",
        {
            "email": "newbie@example.com",
            "password": "long-enough-pw",
            "role": "user",
            "display_name": "Newbie",
        },
    )
    assert response.status_code == 201
    payload = response.json()["user"]
    assert payload["email"] == "newbie@example.com"
    assert payload["role"] == "user"
    assert payload["status"] == "active"


async def test_create_duplicate_email_conflicts(
    admin_env: UserEnv, user_password: str, api_user: User
) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await post_json(
        admin_env.client,
        "/api/admin/users",
        {"email": api_user.email, "password": "long-enough-pw", "role": "user"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


async def test_create_user_rejects_short_password(admin_env: UserEnv, user_password: str) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await post_json(
        admin_env.client,
        "/api/admin/users",
        {"email": "shorty@example.com", "password": "short", "role": "user"},
    )
    assert response.status_code == 422


async def test_patch_user_role(admin_env: UserEnv, user_password: str, second_user: User) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await mutate_json(
        admin_env.client,
        "PATCH",
        f"/api/admin/users/{second_user.id}",
        {"role": "admin"},
    )
    assert response.status_code == 200
    assert response.json()["user"]["role"] == "admin"


async def test_admin_cannot_change_own_role(admin_env: UserEnv, user_password: str) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await mutate_json(
        admin_env.client,
        "PATCH",
        f"/api/admin/users/{admin_env.user.id}",
        {"role": "user"},
    )
    assert response.status_code == 422


async def test_demote_other_admin_leaves_caller_as_admin(
    admin_env: UserEnv,
    user_password: str,
    second_user: User,
    db_session: AsyncSession,
) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    promoted = await mutate_json(
        admin_env.client,
        "PATCH",
        f"/api/admin/users/{second_user.id}",
        {"role": "admin"},
    )
    assert promoted.status_code == 200

    demoted = await mutate_json(
        admin_env.client,
        "PATCH",
        f"/api/admin/users/{second_user.id}",
        {"role": "user"},
    )
    assert demoted.status_code == 200
    assert demoted.json()["user"]["role"] == "user"
    await db_session.refresh(second_user)
    assert second_user.role == "user"


async def test_reset_password_allows_new_login(
    admin_env: UserEnv,
    user_password: str,
    second_user: User,
    second_password: str,
) -> None:
    import httpx

    await login(admin_env.client, admin_env.user.email, user_password)
    response = await post_json(
        admin_env.client,
        f"/api/admin/users/{second_user.id}/reset-password",
        {"password": "brand-new-secret"},
    )
    assert response.status_code == 200

    transport = httpx.ASGITransport(app=admin_env.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as other:
        login_response = await login(other, second_user.email, "brand-new-secret")
        assert login_response.status_code == 200
        old_login = await login(other, second_user.email, second_password)
        assert old_login.status_code == 401


async def test_deactivate_revokes_tokens(
    admin_env: UserEnv,
    user_password: str,
    second_user: User,
    db_session: AsyncSession,
) -> None:
    _raw, token = await create_token(
        db_session, org_id=second_user.org_id, user_id=second_user.id, label="keep"
    )
    await db_session.commit()
    target_id = second_user.id
    token_id = token.id

    await login(admin_env.client, admin_env.user.email, user_password)
    response = await post_json(admin_env.client, f"/api/admin/users/{target_id}/deactivate")
    assert response.status_code == 200
    assert response.json()["user"]["status"] == "deactivated"

    db_session.expire_all()
    refreshed = await db_session.get(User, target_id)
    assert refreshed is not None
    assert refreshed.status == "deactivated"
    token_row = await db_session.get(CamToken, token_id)
    assert token_row is not None
    assert token_row.revoked_at is not None


async def test_cannot_deactivate_self(admin_env: UserEnv, user_password: str) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await post_json(admin_env.client, f"/api/admin/users/{admin_env.user.id}/deactivate")
    assert response.status_code == 422


async def test_list_user_tokens_and_activity(
    admin_env: UserEnv,
    user_password: str,
    second_user: User,
    db_session: AsyncSession,
) -> None:
    await create_token(
        db_session, org_id=second_user.org_id, user_id=second_user.id, label="agent-x"
    )
    await db_session.commit()

    await login(admin_env.client, admin_env.user.email, user_password)
    tokens = await admin_env.client.get(f"/api/admin/users/{second_user.id}/tokens")
    assert tokens.status_code == 200
    assert any(t["label"] == "agent-x" for t in tokens.json()["tokens"])

    activity = await admin_env.client.get(f"/api/admin/users/{second_user.id}/activity")
    assert activity.status_code == 200
    assert activity.json()["entries"] == []


async def test_unknown_user_returns_404(admin_env: UserEnv, user_password: str) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await admin_env.client.get(f"/api/admin/users/{uuid.uuid4()}")
    assert response.status_code == 404
