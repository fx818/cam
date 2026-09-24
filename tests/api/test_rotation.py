import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import UserEnv, login, mutate_json
from cam.db.models import Connection, Service
from cam.upstream import ConnectionTarget, SessionPool
from fixtures.mock_mcp import MockMCPHandle


async def test_rotate_org_credential_closes_pooled_session(
    admin_env: UserEnv,
    user_password: str,
    mock_mcp: MockMCPHandle,
    db_session: AsyncSession,
) -> None:
    from cam.registry.services import register_global_mcp

    service, connection = await register_global_mcp(
        db_session,
        slug=f"rot-{uuid.uuid4().hex[:8]}",
        name="Rotate Global",
        url=mock_mcp.streamable_url,
        transport="streamable-http",
        auth_type="none",
    )
    await db_session.commit()
    pool: SessionPool = admin_env.app.state.pool
    target = ConnectionTarget(
        service_id=str(service.id),
        connection_id=str(connection.id),
        url=mock_mcp.streamable_url,
        transport="streamable-http",
    )
    session = await pool.session(target)
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await mutate_json(
        admin_env.client,
        "POST",
        f"/api/connections/{connection.id}/rotate",
        {"secret": "rotated-admin-secret"},
    )
    assert response.status_code == 200, response.json()
    assert session.closed is True


async def test_rotate_own_connection_reencrypts_with_org_key(
    env_user: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
    db_session: AsyncSession,
) -> None:
    from cam.connections.orgs import decrypt_for_org

    service, connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    response = await mutate_json(
        env_user.client,
        "POST",
        f"/api/connections/{connection.id}/rotate",
        {"secret": "rotated-personal-secret"},
    )
    assert response.status_code == 200, response.json()
    await db_session.refresh(connection)
    assert connection.secret_enc is not None
    decrypted = await decrypt_for_org(db_session, service.org_id, connection.secret_enc)
    assert decrypted.plaintext == b"rotated-personal-secret"
    assert connection.key_version == decrypted.key_version


async def test_rotate_resets_needs_reconnect(
    env_user: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
    db_session: AsyncSession,
) -> None:

    _service, connection = personal_service
    connection.status = "needs_reconnect"
    await db_session.commit()
    await login(env_user.client, env_user.user.email, user_password)
    response = await mutate_json(
        env_user.client,
        "POST",
        f"/api/connections/{connection.id}/rotate",
        {"secret": "rotated-again-secret"},
    )
    assert response.status_code == 200
    assert response.json()["connection"]["status"] == "connected"
