"""Credential resolution: turn a stored connection secret into call headers.

Access checks happen first (see `cam.access.resolution`); this module only
decrypts the org credential the caller is already allowed to use. Credentials
are injected into the upstream request after any audit capture, never into
tool arguments.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from cam.connections.orgs import decrypt_for_org
from cam.db.models import Connection, Service


class CredentialError(Exception):
    """No usable credential is stored for a service that needs one."""


def _decode(plaintext: bytes) -> str:
    return plaintext.decode("utf-8")


async def resolve_credential_headers(
    session: AsyncSession, *, service: Service, connection: Connection
) -> dict[str, str]:
    auth_type = service.auth_type
    if auth_type == "none":
        return {}
    if auth_type not in ("static-header", "bearer"):
        raise CredentialError(f"auth type {auth_type!r} is not supported yet")
    if connection.secret_enc is None:
        raise CredentialError("no credential stored for this connection")
    result = await decrypt_for_org(session, service.org_id, connection.secret_enc)
    secret = _decode(result.plaintext)
    if auth_type == "bearer":
        return {"Authorization": f"Bearer {secret}"}
    header_name = str(service.auth_config.get("header_name") or "").strip()
    if not header_name:
        raise CredentialError("static-header auth is missing its header name")
    return {header_name: secret}
