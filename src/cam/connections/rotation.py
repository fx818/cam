"""Credential rotation: replace a stored secret without re-registering.

The service row keeps its URL, auth type and tools; only the connection's
`secret_enc` is re-encrypted under the org's current data key (`key_version`
follows that key) and its expiry is cleared. Callers close the pooled session
for the (service, connection) right after committing so the next call uses
the new credential.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from cam.db.models import Connection


class RotationError(Exception):
    """The connection cannot be rotated (unknown service, empty secret)."""


async def rotate_connection_secret(
    session: AsyncSession, connection: Connection, *, secret: str
) -> Connection:
    stripped = secret.strip()
    if not stripped:
        raise RotationError("secret must not be empty")
    from cam.connections.orgs import encrypt_for_org

    encrypted, key_version = await encrypt_for_org(session, connection.org_id, stripped)
    connection.secret_enc = encrypted
    connection.key_version = key_version
    connection.status = "connected"
    connection.expires_at = None
    await session.flush()
    return connection
