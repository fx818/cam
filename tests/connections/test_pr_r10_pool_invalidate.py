"""PR R10: after OAuth tokens change, the pooled MCP session must be dropped.

MCP headers are fixed when a session opens, and the pool is keyed only by
``(service_id, connection_id)``. Without an explicit invalidate, CAM keeps
sending the old ``Authorization`` header after a refresh or reconnect.
``refresh_if_needed`` / ``complete_authorize`` accept an ``invalidate`` callback
(and ``refresh_if_needed`` returns whether it refreshed) so callers can drop
the pooled session without this module importing the pool.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from cam.accounts.users import create_user
from cam.config import Settings
from cam.connections.oauth import build_pkce_pair, complete_authorize, refresh_if_needed
from cam.connections.orgs import encrypt_for_org, ensure_org
from cam.db.models import Connection, OAuthState, Service
from fixtures.mock_oauth import MockOAuthHandle
from fixtures.mock_rest import MockRestHandle


async def _make_oauth_service(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    mock_rest: MockRestHandle,
    mock_oauth: MockOAuthHandle,
    refresh_plain: str,
    expires_at: datetime,
) -> tuple[Service, Connection]:
    access_blob, key_version = await encrypt_for_org(
        db, org_id, json.dumps({"access_token": "tok-old", "token_type": "Bearer"})
    )
    refresh_blob, refresh_version = await encrypt_for_org(db, org_id, refresh_plain)
    service = Service(
        org_id=org_id,
        tier="global",
        owner_user_id=None,
        slug=f"r10-{uuid.uuid4().hex[:10]}",
        name="Mock OAuth",
        kind="rest",
        url=mock_rest.base_url,
        auth_type="oauth",
        auth_config={
            "oauth": "plain",
            "authorization_endpoint": f"{mock_oauth.base_url}/authorize",
            "token_endpoint": f"{mock_oauth.base_url}/token",
            "client_id": mock_oauth.client_id,
        },
        visibility="all",
        status="active",
        health="healthy",
    )
    db.add(service)
    await db.flush()
    connection = Connection(
        org_id=org_id,
        service_id=service.id,
        user_id=None,
        secret_enc=access_blob,
        refresh_enc=refresh_blob,
        key_version=refresh_version,
        expires_at=expires_at,
        status="connected",
    )
    db.add(connection)
    await db.flush()
    await db.commit()
    assert key_version == refresh_version
    return service, connection


async def test_refresh_invalidates_pool_after_storing_tokens(
    db_session: AsyncSession, mock_rest: MockRestHandle, mock_oauth: MockOAuthHandle
) -> None:
    org = await ensure_org(db_session, slug=f"r10-{uuid.uuid4().hex[:10]}")
    await db_session.commit()
    now = datetime.now(UTC)
    service, connection = await _make_oauth_service(
        db_session,
        org_id=org.id,
        mock_rest=mock_rest,
        mock_oauth=mock_oauth,
        refresh_plain="rt1",
        expires_at=now + timedelta(seconds=10),
    )
    mock_oauth.refresh_tokens["rt1"] = "alice"

    calls: list[tuple[str, str]] = []

    async def invalidate(service_id: str, connection_id: str) -> None:
        calls.append((service_id, connection_id))

    refreshed = await refresh_if_needed(
        db_session,
        service=service,
        connection=connection,
        base_url=Settings().base_url,
        invalidate=invalidate,
    )

    assert refreshed is True
    assert calls == [(str(service.id), str(connection.id))]
    assert connection.status == "connected"
    assert connection.expires_at is not None
    assert connection.expires_at > now


async def test_refresh_does_not_invalidate_when_not_expiring(
    db_session: AsyncSession, mock_rest: MockRestHandle, mock_oauth: MockOAuthHandle
) -> None:
    org = await ensure_org(db_session, slug=f"r10-{uuid.uuid4().hex[:10]}")
    await db_session.commit()
    now = datetime.now(UTC)
    service, connection = await _make_oauth_service(
        db_session,
        org_id=org.id,
        mock_rest=mock_rest,
        mock_oauth=mock_oauth,
        refresh_plain="rt1",
        expires_at=now + timedelta(hours=1),
    )

    calls: list[tuple[str, str]] = []

    async def invalidate(service_id: str, connection_id: str) -> None:
        calls.append((service_id, connection_id))

    refreshed = await refresh_if_needed(
        db_session,
        service=service,
        connection=connection,
        base_url=Settings().base_url,
        invalidate=invalidate,
    )

    assert refreshed is False
    assert calls == []


async def test_complete_authorize_invalidates_pool(
    db_session: AsyncSession, mock_rest: MockRestHandle, mock_oauth: MockOAuthHandle
) -> None:
    org = await ensure_org(db_session, slug=f"r10-{uuid.uuid4().hex[:10]}")
    await db_session.commit()
    user = await create_user(
        db_session,
        email=f"r10-{uuid.uuid4().hex[:8]}@example.com",
        password="pw-1234567890",
        role="user",
        org_slug=org.slug,
    )
    await db_session.commit()
    now = datetime.now(UTC)
    service, connection = await _make_oauth_service(
        db_session,
        org_id=org.id,
        mock_rest=mock_rest,
        mock_oauth=mock_oauth,
        refresh_plain="rt-unused",
        expires_at=now + timedelta(hours=1),
    )

    verifier, challenge = build_pkce_pair()
    state = secrets.token_urlsafe(16)
    redirect_uri = f"{Settings().base_url.rstrip('/')}/api/oauth/callback"
    db_session.add(
        OAuthState(
            org_id=org.id,
            user_id=user.id,
            service_id=service.id,
            state=state,
            pkce_verifier=verifier,
            expires_at=now + timedelta(minutes=5),
        )
    )
    await db_session.commit()

    async with httpx.AsyncClient(timeout=10.0) as external:
        authorize_resp = await external.get(
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
    assert authorize_resp.status_code == 302
    code = parse_qs(urlparse(authorize_resp.headers["location"]).query)["code"][0]

    calls: list[tuple[str, str]] = []

    async def invalidate(service_id: str, connection_id: str) -> None:
        calls.append((service_id, connection_id))

    returned = await complete_authorize(
        db_session,
        service=service,
        code=code,
        state=state,
        base_url=Settings().base_url,
        invalidate=invalidate,
    )
    await db_session.commit()

    assert returned.id == connection.id
    assert calls == [(str(service.id), str(returned.id))]
    assert returned.status == "connected"
    assert returned.secret_enc is not None
