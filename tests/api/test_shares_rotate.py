from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import UserEnv, login, mutate_json, user_client
from cam.connections.orgs import decrypt_for_org
from cam.db.models import Connection, Service, Share, User


async def test_share_lifecycle(
    env_user: UserEnv,
    user_password: str,
    second_user: User,
    second_password: str,
    personal_service: tuple[Service, Connection],
    db_session: AsyncSession,
) -> None:
    service, connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)

    added = await mutate_json(
        env_user.client,
        "POST",
        f"/api/connections/{connection.id}/shares",
        {"email": second_user.email},
    )
    assert added.status_code == 201
    share_id = added.json()["share"]["id"]
    assert added.json()["share"]["grantee"]["email"] == second_user.email

    listed = await env_user.client.get(f"/api/connections/{connection.id}/shares")
    assert listed.status_code == 200
    active = [s for s in listed.json()["shares"] if s["revoked_at"] is None]
    assert len(active) == 1

    async with user_client(env_user.app) as other:
        await login(other, second_user.email, second_password)
        sees = await other.get(f"/api/services/{env_user.user.handle}/{service.slug}")
        assert sees.status_code == 200

    revoked = await mutate_json(env_user.client, "DELETE", f"/api/shares/{share_id}")
    assert revoked.status_code == 200
    assert revoked.json() == {"ok": True}

    async with user_client(env_user.app) as other:
        await login(other, second_user.email, second_password)
        lost = await other.get(f"/api/services/{env_user.user.handle}/{service.slug}")
        assert lost.status_code == 404


async def test_duplicate_share_rejected(
    env_user: UserEnv,
    user_password: str,
    second_user: User,
    personal_service: tuple[Service, Connection],
    active_share: Share,
) -> None:
    _service, connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    response = await mutate_json(
        env_user.client,
        "POST",
        f"/api/connections/{connection.id}/shares",
        {"email": second_user.email},
    )
    assert response.status_code == 400
    assert "already shared" in response.json()["error"]["message"]


async def test_share_unknown_email(
    env_user: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
) -> None:
    _service, connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    response = await mutate_json(
        env_user.client,
        "POST",
        f"/api/connections/{connection.id}/shares",
        {"email": "ghost@example.com"},
    )
    assert response.status_code == 400


async def test_share_with_self_rejected(
    env_user: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
) -> None:
    _service, connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    response = await mutate_json(
        env_user.client,
        "POST",
        f"/api/connections/{connection.id}/shares",
        {"email": env_user.user.email},
    )
    assert response.status_code == 400
    assert "yourself" in response.json()["error"]["message"]


async def test_rotate_secret_by_owner(
    env_user: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
    db_session: AsyncSession,
) -> None:
    _service, connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    response = await mutate_json(
        env_user.client,
        "POST",
        f"/api/connections/{connection.id}/rotate",
        {"secret": "brand-new-secret"},
    )
    assert response.status_code == 200
    assert response.json()["connection"]["status"] == "connected"

    await db_session.refresh(connection)
    assert connection.secret_enc is not None
    decrypted = await decrypt_for_org(db_session, connection.org_id, connection.secret_enc)
    assert decrypted.plaintext == b"brand-new-secret"
    assert connection.expires_at is None


async def test_rotate_secret_stranger_404(
    env_user: UserEnv,
    user_password: str,
    second_user: User,
    second_password: str,
    personal_service: tuple[Service, Connection],
) -> None:
    _service, connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    async with user_client(env_user.app) as other:
        await login(other, second_user.email, second_password)
        response = await mutate_json(
            other, "POST", f"/api/connections/{connection.id}/rotate", {"secret": "x"}
        )
        assert response.status_code == 404


async def test_org_connection_only_admin(
    env_user: UserEnv,
    admin_env: UserEnv,
    user_password: str,
    global_service: tuple[Service, Connection],
    db_session: AsyncSession,
) -> None:
    _service, connection = global_service
    await login(env_user.client, env_user.user.email, user_password)
    forbidden = await mutate_json(
        env_user.client, "POST", f"/api/connections/{connection.id}/rotate", {"secret": "x"}
    )
    assert forbidden.status_code == 404

    await login(admin_env.client, admin_env.user.email, user_password)
    allowed = await mutate_json(
        admin_env.client,
        "POST",
        f"/api/connections/{connection.id}/rotate",
        {"secret": "admin-new-secret"},
    )
    assert allowed.status_code == 200
    await db_session.refresh(connection)
    decrypted = await decrypt_for_org(db_session, connection.org_id, connection.secret_enc)  # type: ignore[arg-type]
    assert decrypted.plaintext == b"admin-new-secret"


async def test_rotate_requires_secret(
    env_user: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
) -> None:
    _service, connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    response = await mutate_json(
        env_user.client, "POST", f"/api/connections/{connection.id}/rotate", {"secret": ""}
    )
    assert response.status_code == 422


async def test_shares_require_auth(env_user: UserEnv) -> None:
    zero_id = "00000000-0000-0000-0000-000000000000"
    response = await env_user.client.get(f"/api/connections/{zero_id}/shares")
    assert response.status_code == 401
