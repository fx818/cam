"""R34: deleting a service must also clear its service-level secret and revoke
outstanding OAuth authorizations (pending states), not just connection secrets.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import UserEnv, login, mutate_json
from cam.db.models import Connection, OAuthState, Service


async def test_delete_service_clears_secrets_and_oauth_states(
    env_user: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
    db_session: AsyncSession,
) -> None:
    service, connection = personal_service
    service.auth_secret_enc = b"encrypted-service-secret"
    db_session.add(
        OAuthState(
            org_id=service.org_id,
            user_id=env_user.user.id,
            service_id=service.id,
            state=f"state-{uuid.uuid4().hex}",
            pkce_verifier="verifier",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db_session.commit()

    await login(env_user.client, env_user.user.email, user_password)
    ref = f"{env_user.user.handle}/{service.slug}"
    response = await mutate_json(env_user.client, "DELETE", f"/api/services/{ref}")
    assert response.status_code == 200, response.text

    await db_session.refresh(service)
    await db_session.refresh(connection)
    assert service.status == "deleted"
    assert service.auth_secret_enc is None
    assert connection.status == "disabled"
    assert connection.secret_enc is None
    assert connection.refresh_enc is None

    remaining = list(
        (
            await db_session.execute(
                select(OAuthState.id).where(OAuthState.service_id == service.id)
            )
        ).scalars()
    )
    assert remaining == []


async def test_delete_service_does_not_touch_other_services_oauth_states(
    env_user: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
    db_session: AsyncSession,
) -> None:
    service, _connection = personal_service
    other = Service(
        org_id=service.org_id,
        tier="personal",
        owner_user_id=env_user.user.id,
        slug=f"other-{uuid.uuid4().hex[:8]}",
        name="Other",
        kind="mcp",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="oauth",
        auth_config={},
        visibility="all",
        status="active",
        health="unknown",
    )
    db_session.add(other)
    await db_session.flush()
    other_state = OAuthState(
        org_id=service.org_id,
        user_id=env_user.user.id,
        service_id=other.id,
        state=f"state-{uuid.uuid4().hex}",
        pkce_verifier="verifier",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(other_state)
    await db_session.commit()

    await login(env_user.client, env_user.user.email, user_password)
    ref = f"{env_user.user.handle}/{service.slug}"
    response = await mutate_json(env_user.client, "DELETE", f"/api/services/{ref}")
    assert response.status_code == 200, response.text

    survivor = (
        await db_session.execute(select(OAuthState.id).where(OAuthState.service_id == other.id))
    ).scalar_one_or_none()
    assert survivor == other_state.id
