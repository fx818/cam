"""R11 — MCP resync must resolve OAuth credentials, not send empty headers.

`build_connection_target` only resolved a credential when
`auth_type != "oauth"`, so OAuth services (e.g. the Notion template) were
contacted with no Authorization header and could never import/resync tools.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cam.accounts.users import create_user
from cam.connections.orgs import encrypt_for_org, ensure_org
from cam.db.models import Connection, Service
from cam.registry.resync import ResyncError, build_connection_target, run_service_resync
from fixtures.mock_mcp import MockMCPHandle


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _oauth_template(
    db_session: AsyncSession, *, url: str, token_json: str | None
) -> tuple[Service, Connection]:
    org = await ensure_org(db_session)
    connector = await create_user(
        db_session,
        org_id=org.id,
        email=f"oauth-{uuid.uuid4().hex[:10]}@example.com",
        password="pw-whatever",
        role="user",
    )
    service = Service(
        org_id=org.id,
        tier="template",
        owner_user_id=None,
        slug=_unique("oauthtpl"),
        name="Notion",
        description="oauth template",
        kind="mcp",
        url=url,
        transport="streamable-http",
        auth_type="oauth",
        auth_config={"oauth": "plain", "client_id": "cid"},
        visibility="all",
        status="active",
        health="unknown",
    )
    db_session.add(service)
    await db_session.flush()

    connection = Connection(
        org_id=org.id,
        service_id=service.id,
        user_id=connector.id,
        status="connected",
        expires_at=None,
    )
    if token_json is not None:
        blob, key_version = await encrypt_for_org(db_session, org.id, token_json)
        connection.secret_enc = blob
        connection.key_version = key_version
    db_session.add(connection)
    await db_session.flush()
    return service, connection


async def test_oauth_connection_resolves_bearer_header(db_session: AsyncSession) -> None:
    service, connection = await _oauth_template(
        db_session,
        url="http://127.0.0.1:9/mcp",
        token_json=json.dumps({"access_token": "tok-abc", "token_type": "Bearer"}),
    )
    target = await build_connection_target(db_session, service, connection)
    assert target.headers.get("Authorization") == "Bearer tok-abc"


async def test_oauth_connection_without_credential_errors_clearly(
    db_session: AsyncSession,
) -> None:
    service, connection = await _oauth_template(
        db_session, url="http://127.0.0.1:9/mcp", token_json=None
    )
    with pytest.raises(ResyncError, match="credential"):
        await build_connection_target(db_session, service, connection)


async def test_oauth_resync_reaches_authed_upstream(
    db_session: AsyncSession, mock_mcp_authed: MockMCPHandle
) -> None:
    service, connection = await _oauth_template(
        db_session,
        url=mock_mcp_authed.streamable_url,
        token_json=json.dumps({"access_token": "test-secret", "token_type": "Bearer"}),
    )
    outcome = await run_service_resync(db_session, service, connection=connection)
    assert outcome.status == "done", outcome.error
    assert "echo" in outcome.added
