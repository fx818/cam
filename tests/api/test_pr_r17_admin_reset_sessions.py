"""R17 — an admin-initiated password reset revokes the target's sessions.

Before: resetting a password changed the hash but left existing dashboard
session cookies valid. Now the reset bumps the user's `session_version`, so
`cam.api.deps.load_identity` rejects any cookie issued before the reset.
"""

from __future__ import annotations

import httpx

from api.helpers import UserEnv, login, post_json
from cam.db.models import User


async def test_admin_password_reset_revokes_target_sessions(
    admin_env: UserEnv,
    user_password: str,
    second_user: User,
    second_password: str,
) -> None:
    transport = httpx.ASGITransport(app=admin_env.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as victim:
        logged_in = await login(victim, second_user.email, second_password)
        assert logged_in.status_code == 200
        assert (await victim.get("/api/auth/me")).status_code == 200

        await login(admin_env.client, admin_env.user.email, user_password)
        reset = await post_json(
            admin_env.client,
            f"/api/admin/users/{second_user.id}/reset-password",
            {"password": "brand-new-secret"},
        )
        assert reset.status_code == 200

        # The victim's pre-reset cookie is now rejected.
        assert (await victim.get("/api/auth/me")).status_code == 401

        # A fresh login with the new password works.
        fresh = await login(victim, second_user.email, "brand-new-secret")
        assert fresh.status_code == 200
        assert (await victim.get("/api/auth/me")).status_code == 200
