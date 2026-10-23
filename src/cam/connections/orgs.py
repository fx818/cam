import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.connections.crypto import (
    DecryptedSecret,
    MasterKeyRing,
    decrypt_secret,
    encrypt_secret,
    generate_data_key,
    get_default_key_ring,
    unwrap_data_key,
    wrap_data_key,
)
from cam.db.models import Org

DEFAULT_ORG_SLUG = "indiamart"
DEFAULT_ORG_NAME = "IndiaMART"


class OrgNotFoundError(Exception):
    pass


def _provision_data_key(org: Org, ring: MasterKeyRing) -> bytes:
    blob = wrap_data_key(generate_data_key(), ring, version=1)
    org.data_key_enc = blob
    return blob


async def get_org_by_slug(session: AsyncSession, slug: str) -> Org | None:
    result = await session.execute(select(Org).where(Org.slug == slug))
    return result.scalar_one_or_none()


async def get_org_by_id(session: AsyncSession, org_id: uuid.UUID) -> Org | None:
    """Fetch an existing org by primary key; never creates one."""
    return await session.get(Org, org_id)


async def get_org(session: AsyncSession, org_id: uuid.UUID) -> Org:
    """Fetch an existing org by primary key, raising when it is absent.

    Admin-scoped operations use this instead of `ensure_org` so they never
    silently fall back to the default org (R02).
    """
    org = await get_org_by_id(session, org_id)
    if org is None:
        raise OrgNotFoundError(f"org {org_id} does not exist")
    return org


async def ensure_org(
    session: AsyncSession,
    *,
    slug: str = DEFAULT_ORG_SLUG,
    name: str = DEFAULT_ORG_NAME,
    settings: dict[str, Any] | None = None,
    ring: MasterKeyRing | None = None,
) -> Org:
    """Bootstrap lookup: return the named org, creating it if missing.

    This is the *only* default-org creation path and is reserved for
    authentication/bootstrap flows (first login, CLI `create-admin`) and
    service-layer test fixtures. Admin-reachable operations must take an
    explicit `org_id` derived from the authenticated admin instead (R02/R25).
    """
    key_ring = ring if ring is not None else get_default_key_ring()
    org = await get_org_by_slug(session, slug)
    if org is None:
        org = Org(slug=slug, name=name, settings=settings if settings is not None else {})
        session.add(org)
        await session.flush()
    if org.data_key_enc is None:
        _provision_data_key(org, key_ring)
        await session.flush()
    return org


async def _load_data_key(
    session: AsyncSession, org_id: uuid.UUID, ring: MasterKeyRing
) -> tuple[int, bytes]:
    org = await session.get(Org, org_id)
    if org is None:
        raise OrgNotFoundError(f"org {org_id} does not exist")
    blob = org.data_key_enc
    if blob is None:
        blob = _provision_data_key(org, ring)
        await session.flush()
    return unwrap_data_key(blob, ring)


async def get_org_data_key(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    ring: MasterKeyRing | None = None,
) -> tuple[int, bytes]:
    key_ring = ring if ring is not None else get_default_key_ring()
    return await _load_data_key(session, org_id, key_ring)


async def encrypt_for_org(
    session: AsyncSession,
    org_id: uuid.UUID,
    plaintext: str | bytes,
    *,
    ring: MasterKeyRing | None = None,
) -> tuple[bytes, int]:
    key_ring = ring if ring is not None else get_default_key_ring()
    key_version, data_key = await _load_data_key(session, org_id, key_ring)
    data = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
    return encrypt_secret(data, data_key, key_version=key_version), key_version


async def decrypt_for_org(
    session: AsyncSession,
    org_id: uuid.UUID,
    blob: bytes,
    *,
    ring: MasterKeyRing | None = None,
) -> DecryptedSecret:
    key_ring = ring if ring is not None else get_default_key_ring()
    _, data_key = await _load_data_key(session, org_id, key_ring)
    return decrypt_secret(blob, data_key)
