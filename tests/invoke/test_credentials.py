"""Unit tests for credential resolution into upstream call headers."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cam.access.credentials import CredentialError, resolve_credential_headers
from cam.connections.orgs import encrypt_for_org, ensure_org
from cam.db.models import Connection, Service


async def _service(
    db_session: AsyncSession,
    org_id: uuid.UUID,
    *,
    auth_type: str,
    auth_config: dict[str, str] | None = None,
) -> Service:
    service = Service(
        org_id=org_id,
        tier="global",
        owner_user_id=None,
        slug=f"cred-{uuid.uuid4().hex[:10]}",
        name="Cred Test",
        kind="mcp",
        url="http://127.0.0.1:9/mcp",
        transport="streamable-http",
        auth_type=auth_type,
        auth_config=auth_config or {},
    )
    db_session.add(service)
    await db_session.flush()
    return service


async def _connection(
    db_session: AsyncSession,
    org_id: uuid.UUID,
    service: Service,
    *,
    secret: str | None = None,
) -> Connection:
    secret_enc: bytes | None = None
    if secret is not None:
        blob, _version = await encrypt_for_org(db_session, org_id, secret)
        secret_enc = blob
    connection = Connection(
        org_id=org_id,
        service_id=service.id,
        user_id=None,
        secret_enc=secret_enc,
        status="connected",
    )
    db_session.add(connection)
    await db_session.flush()
    return connection


async def test_none_auth_returns_no_headers(db_session: AsyncSession) -> None:
    org = await ensure_org(db_session)
    service = await _service(db_session, org.id, auth_type="none")
    connection = await _connection(db_session, org.id, service)
    headers = await resolve_credential_headers(db_session, service=service, connection=connection)
    assert headers == {}


async def test_bearer_auth_builds_authorization_header(db_session: AsyncSession) -> None:
    org = await ensure_org(db_session)
    service = await _service(db_session, org.id, auth_type="bearer")
    connection = await _connection(db_session, org.id, service, secret="tok-123")
    headers = await resolve_credential_headers(db_session, service=service, connection=connection)
    assert headers == {"Authorization": "Bearer tok-123"}


async def test_static_header_auth_uses_configured_name(db_session: AsyncSession) -> None:
    org = await ensure_org(db_session)
    service = await _service(
        db_session, org.id, auth_type="static-header", auth_config={"header_name": "X-Api-Key"}
    )
    connection = await _connection(db_session, org.id, service, secret="key-456")
    headers = await resolve_credential_headers(db_session, service=service, connection=connection)
    assert headers == {"X-Api-Key": "key-456"}


async def test_missing_secret_raises(db_session: AsyncSession) -> None:
    org = await ensure_org(db_session)
    service = await _service(db_session, org.id, auth_type="bearer")
    connection = await _connection(db_session, org.id, service)
    with pytest.raises(CredentialError, match="no credential"):
        await resolve_credential_headers(db_session, service=service, connection=connection)


async def test_oauth_auth_builds_bearer_header(db_session: AsyncSession) -> None:
    org = await ensure_org(db_session)
    service = await _service(db_session, org.id, auth_type="oauth", auth_config={"oauth": "mcp"})
    connection = await _connection(
        db_session,
        org.id,
        service,
        secret='{"access_token": "oauth-tok", "token_type": "Bearer"}',
    )
    headers = await resolve_credential_headers(db_session, service=service, connection=connection)
    assert headers == {"Authorization": "Bearer oauth-tok"}


async def test_oauth_missing_secret_raises(db_session: AsyncSession) -> None:
    org = await ensure_org(db_session)
    service = await _service(db_session, org.id, auth_type="oauth", auth_config={"oauth": "mcp"})
    connection = await _connection(db_session, org.id, service)
    with pytest.raises(CredentialError, match="no credential"):
        await resolve_credential_headers(db_session, service=service, connection=connection)


async def test_static_header_without_name_raises(db_session: AsyncSession) -> None:
    org = await ensure_org(db_session)
    service = await _service(db_session, org.id, auth_type="static-header")
    connection = await _connection(db_session, org.id, service, secret="key")
    with pytest.raises(CredentialError, match="header name"):
        await resolve_credential_headers(db_session, service=service, connection=connection)
