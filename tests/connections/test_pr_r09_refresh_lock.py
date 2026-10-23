"""PR R09: the locked connection row must be reloaded fresh, not read from the
identity map, before the post-lock refresh re-check.

Regression: without ``populate_existing`` the ``SELECT ... FOR UPDATE`` returns
the stale identity-mapped object (old ``expires_at`` / rotated ``refresh_enc``),
so a caller whose refresh token was already rotated by a concurrent refresh
re-exchanges it, fails, and wrongly flags a healthy connection
``needs_reconnect``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cam.config import Settings
from cam.connections.oauth import refresh_if_needed
from cam.connections.orgs import encrypt_for_org, ensure_org
from cam.db.models import Connection, Service
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
        slug=f"r09-{uuid.uuid4().hex[:10]}",
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


async def test_locked_row_reloaded_fresh_skips_second_exchange(
    db_session: AsyncSession, mock_rest: MockRestHandle, mock_oauth: MockOAuthHandle
) -> None:
    org = await ensure_org(db_session, slug=f"r09-{uuid.uuid4().hex[:10]}")
    await db_session.commit()
    now = datetime.now(UTC)
    service, connection = await _make_oauth_service(
        db_session,
        org_id=org.id,
        mock_rest=mock_rest,
        mock_oauth=mock_oauth,
        refresh_plain="rt-stale",
        expires_at=now + timedelta(seconds=10),
    )

    # A concurrent caller already rotated the refresh token and extended the
    # expiry, committing under our feet: the in-memory object still holds the
    # now-invalid `rt-stale`; the row holds `rt-fresh`.
    mock_oauth.refresh_tokens.pop("rt-stale", None)
    mock_oauth.refresh_tokens["rt-fresh"] = "alice"
    fresh_blob, fresh_version = await encrypt_for_org(db_session, org.id, "rt-fresh")
    await db_session.execute(
        text(
            "UPDATE connections SET expires_at = :exp, refresh_enc = :enc, key_version = :kv "
            "WHERE id = :id"
        ),
        {
            "exp": now + timedelta(hours=1),
            "enc": fresh_blob,
            "kv": fresh_version,
            "id": connection.id,
        },
    )

    token_requests_before = mock_oauth.token_requests
    refreshed = await refresh_if_needed(
        db_session, service=service, connection=connection, base_url=Settings().base_url
    )

    assert refreshed is False
    assert mock_oauth.token_requests == token_requests_before
    assert connection.status == "connected"
    assert connection.expires_at is not None
    assert connection.expires_at > now + timedelta(minutes=30)


async def test_refresh_skips_when_locked_row_not_connected(
    db_session: AsyncSession, mock_rest: MockRestHandle, mock_oauth: MockOAuthHandle
) -> None:
    org = await ensure_org(db_session, slug=f"r09-{uuid.uuid4().hex[:10]}")
    await db_session.commit()
    now = datetime.now(UTC)
    service, connection = await _make_oauth_service(
        db_session,
        org_id=org.id,
        mock_rest=mock_rest,
        mock_oauth=mock_oauth,
        refresh_plain="rt-stale",
        expires_at=now + timedelta(seconds=10),
    )
    # A concurrent transaction disabled the connection.
    await db_session.execute(
        text("UPDATE connections SET status = 'disabled' WHERE id = :id"),
        {"id": connection.id},
    )

    token_requests_before = mock_oauth.token_requests
    refreshed = await refresh_if_needed(
        db_session, service=service, connection=connection, base_url=Settings().base_url
    )

    assert refreshed is False
    assert mock_oauth.token_requests == token_requests_before
