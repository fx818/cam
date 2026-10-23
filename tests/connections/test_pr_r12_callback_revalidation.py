"""PR R12: the OAuth callback must not trust stored state alone.

Between ``authorize`` and the callback the initiating user may be deactivated,
the service disabled/soft-deleted, or access revoked. The callback locks the
state row and re-validates user + service + org + authorization before the token
exchange, and purges the pending states so a stale round-trip cannot reactivate
a connection.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import api_client
from cam.accounts.users import create_user
from cam.api.main import create_app
from cam.config import Settings
from cam.connections.oauth import build_pkce_pair
from cam.connections.orgs import ensure_org
from cam.db.models import Connection, OAuthState, Service, User
from fixtures.mock_oauth import MockOAuthHandle
from fixtures.mock_rest import MockRestHandle


class _RecordingPool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def invalidate_connection(self, service_id: str, connection_id: str) -> None:
        self.calls.append((service_id, connection_id))


async def _setup_target(
    db: AsyncSession, mock_rest: MockRestHandle, mock_oauth: MockOAuthHandle
) -> tuple[Service, Connection, User, str, str, str]:
    org = await ensure_org(db, slug=f"r12-{uuid.uuid4().hex[:10]}")
    await db.commit()
    user = await create_user(
        db,
        email=f"r12-{uuid.uuid4().hex[:8]}@example.com",
        password="pw-1234567890",
        role="admin",
        org_slug=org.slug,
    )
    await db.commit()
    service = Service(
        org_id=org.id,
        tier="global",
        owner_user_id=None,
        slug=f"r12-{uuid.uuid4().hex[:10]}",
        name="R12 OAuth",
        kind="rest",
        url=mock_rest.base_url,
        spec_url=mock_rest.spec_url,
        auth_type="oauth",
        auth_config={
            "oauth": "plain",
            "authorization_endpoint": f"{mock_oauth.base_url}/authorize",
            "token_endpoint": f"{mock_oauth.base_url}/token",
            "client_id": mock_oauth.client_id,
        },
        visibility="all",
        status="active",
        health="unknown",
    )
    db.add(service)
    await db.flush()
    connection = Connection(
        org_id=org.id,
        service_id=service.id,
        user_id=None,
        key_version=1,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        status="connected",
    )
    db.add(connection)
    await db.flush()
    verifier, challenge = build_pkce_pair()
    state = secrets.token_urlsafe(16)
    db.add(
        OAuthState(
            org_id=org.id,
            user_id=user.id,
            service_id=service.id,
            state=state,
            pkce_verifier=verifier,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    await db.commit()
    redirect_uri = f"{Settings().base_url.rstrip('/')}/api/oauth/callback"
    return service, connection, user, state, challenge, redirect_uri


async def _authorize_code(
    mock_oauth: MockOAuthHandle, *, state: str, challenge: str, redirect_uri: str
) -> str:
    async with httpx.AsyncClient(timeout=10.0) as external:
        resp = await external.get(
            f"{mock_oauth.base_url}/authorize",
            params={
                "response_type": "code",
                "client_id": mock_oauth.client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "as_user": "alice",
            },
            follow_redirects=False,
        )
    assert resp.status_code == 302
    return parse_qs(urlparse(resp.headers["location"]).query)["code"][0]


async def _state_exists(db: AsyncSession, state: str) -> bool:
    row = (
        await db.execute(select(OAuthState.id).where(OAuthState.state == state))
    ).scalar_one_or_none()
    return row is not None


async def test_callback_rejected_when_initiating_user_deactivated(
    db_session: AsyncSession, mock_rest: MockRestHandle, mock_oauth: MockOAuthHandle
) -> None:
    _service, _connection, user, state, challenge, redirect_uri = await _setup_target(
        db_session, mock_rest, mock_oauth
    )
    code = await _authorize_code(
        mock_oauth, state=state, challenge=challenge, redirect_uri=redirect_uri
    )
    user.status = "deactivated"
    user.deactivated_at = datetime.now(UTC)
    await db_session.commit()

    app = create_app()
    async with api_client(app) as client:
        resp = await client.get(
            f"/api/oauth/callback?code={code}&state={state}", follow_redirects=False
        )
    assert resp.status_code == 401
    assert not await _state_exists(db_session, state)


async def test_callback_rejected_when_service_deleted(
    db_session: AsyncSession, mock_rest: MockRestHandle, mock_oauth: MockOAuthHandle
) -> None:
    service, _connection, _user, state, challenge, redirect_uri = await _setup_target(
        db_session, mock_rest, mock_oauth
    )
    code = await _authorize_code(
        mock_oauth, state=state, challenge=challenge, redirect_uri=redirect_uri
    )
    service.status = "deleted"
    await db_session.commit()

    app = create_app()
    async with api_client(app) as client:
        resp = await client.get(
            f"/api/oauth/callback?code={code}&state={state}", follow_redirects=False
        )
    assert resp.status_code == 404
    assert not await _state_exists(db_session, state)


async def test_callback_rejected_when_service_not_active(
    db_session: AsyncSession, mock_rest: MockRestHandle, mock_oauth: MockOAuthHandle
) -> None:
    service, _connection, _user, state, challenge, redirect_uri = await _setup_target(
        db_session, mock_rest, mock_oauth
    )
    code = await _authorize_code(
        mock_oauth, state=state, challenge=challenge, redirect_uri=redirect_uri
    )
    service.status = "disabled"
    await db_session.commit()

    app = create_app()
    async with api_client(app) as client:
        resp = await client.get(
            f"/api/oauth/callback?code={code}&state={state}", follow_redirects=False
        )
    assert resp.status_code == 404
    assert not await _state_exists(db_session, state)


async def test_callback_completes_and_invalidates_pool(
    db_session: AsyncSession, mock_rest: MockRestHandle, mock_oauth: MockOAuthHandle
) -> None:
    service, connection, _user, state, challenge, redirect_uri = await _setup_target(
        db_session, mock_rest, mock_oauth
    )
    code = await _authorize_code(
        mock_oauth, state=state, challenge=challenge, redirect_uri=redirect_uri
    )

    app = create_app()
    pool = _RecordingPool()
    async with api_client(app) as client:
        app.state.pool = pool
        resp = await client.get(
            f"/api/oauth/callback?code={code}&state={state}", follow_redirects=False
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == f"{Settings().base_url.rstrip('/')}/"

    assert pool.calls == [(str(service.id), str(connection.id))]
    await db_session.refresh(connection)
    assert connection.status == "connected"
    assert connection.secret_enc is not None
    assert not await _state_exists(db_session, state)
