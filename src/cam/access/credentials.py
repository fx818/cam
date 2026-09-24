"""Credential resolution: turn a stored connection secret into call material.

Access checks happen first (see `cam.access.resolution`); this module only
decrypts the credential the caller is already allowed to use. Credentials are
injected into the upstream request after any audit capture, never into tool
arguments. Non-secret placement lives in `service.auth_config`; the secret
itself lives in `connection.secret_enc`, encrypted with the org data key.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from cam.connections.orgs import decrypt_for_org
from cam.db.models import Connection, Service


class CredentialError(Exception):
    """No usable credential is stored for a service that needs one."""


@dataclass(frozen=True, slots=True)
class CredentialMaterial:
    headers: dict[str, str] = field(default_factory=dict[str, str])
    query: dict[str, str] = field(default_factory=dict[str, str])


def _decode(plaintext: bytes) -> str:
    return plaintext.decode("utf-8")


async def _stored_secret(session: AsyncSession, *, service: Service, connection: Connection) -> str:
    if connection.secret_enc is None:
        raise CredentialError("no credential stored for this connection")
    result = await decrypt_for_org(session, service.org_id, connection.secret_enc)
    return _decode(result.plaintext)


async def resolve_credential(
    session: AsyncSession, *, service: Service, connection: Connection
) -> CredentialMaterial:
    auth_type = service.auth_type
    if auth_type == "none":
        return CredentialMaterial()
    if auth_type in ("static-header", "bearer"):
        secret = await _stored_secret(session, service=service, connection=connection)
        if auth_type == "bearer":
            return CredentialMaterial(headers={"Authorization": f"Bearer {secret}"})
        header_name = str(service.auth_config.get("header_name") or "").strip()
        if not header_name:
            raise CredentialError("static-header auth is missing its header name")
        return CredentialMaterial(headers={header_name: secret})
    if auth_type == "api-key":
        secret = await _stored_secret(session, service=service, connection=connection)
        key_name = str(service.auth_config.get("api_key_name") or "").strip()
        if not key_name:
            raise CredentialError("api-key auth is missing its key name")
        location = str(service.auth_config.get("api_key_in") or "header")
        if location == "query":
            return CredentialMaterial(query={key_name: secret})
        if location != "header":
            raise CredentialError("api-key auth location must be header or query")
        return CredentialMaterial(headers={key_name: secret})
    if auth_type == "basic":
        secret = await _stored_secret(session, service=service, connection=connection)
        username = str(service.auth_config.get("username") or "")
        if not username:
            raise CredentialError("basic auth is missing its username")
        token = base64.b64encode(f"{username}:{secret}".encode()).decode("ascii")
        return CredentialMaterial(headers={"Authorization": f"Basic {token}"})
    raise CredentialError(f"auth type {auth_type!r} is not supported yet")


async def resolve_credential_headers(
    session: AsyncSession, *, service: Service, connection: Connection
) -> dict[str, str]:
    material = await resolve_credential(session, service=service, connection=connection)
    return dict(material.headers)
