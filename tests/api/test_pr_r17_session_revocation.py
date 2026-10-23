"""PR R17: dashboard sessions must be revocable.

The cookie carries a signed payload including the user's ``session_version``;
logout bumps it so every previously issued cookie is rejected even though its
signature is still valid. Password changes are admin-only (a locked decision),
so self-service change is out of scope here.
"""

from __future__ import annotations

from api.helpers import UserEnv, login, mutate_json
from cam.auth.sessions import SESSION_COOKIE


async def _login(env: UserEnv, password: str) -> None:
    response = await login(env.client, env.user.email, password)
    assert response.status_code == 200


async def test_logout_revokes_the_session_cookie(env_user: UserEnv, user_password: str) -> None:
    await _login(env_user, user_password)
    stolen = env_user.client.cookies.get(SESSION_COOKIE)
    assert stolen is not None

    out = await mutate_json(env_user.client, "POST", "/api/auth/logout")
    assert out.status_code == 200

    # Replaying the pre-logout cookie must fail: the session version moved on.
    env_user.client.cookies.set(SESSION_COOKIE, stolen)
    me = await env_user.client.get("/api/auth/me")
    assert me.status_code == 401


async def test_self_service_password_change_is_not_exposed(env_user: UserEnv) -> None:
    """Admin-only password changes are a locked decision: no public endpoint."""
    response = await mutate_json(
        env_user.client,
        "POST",
        "/api/auth/password",
        {"current_password": "x", "new_password": "newpw-1234567890"},
    )
    assert response.status_code in (404, 405)
