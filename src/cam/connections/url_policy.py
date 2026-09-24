"""Org URL allowlist rows feeding outbound URL policy checks.

The allowlist exempts exact hostnames or CIDR ranges from the blocked-range
rules for personal-tier destinations. Patterns are normalized once on write
(CIDRs canonicalized, hostnames lowercased) so matching stays exact.
"""

from __future__ import annotations

import ipaddress
import re
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.connections.orgs import DEFAULT_ORG_SLUG, ensure_org
from cam.db.models import UrlAllowlist
from cam.upstream.url import UrlPolicy

_HOSTNAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


class AllowlistError(Exception):
    pass


def normalize_pattern(pattern: str) -> str:
    stripped = pattern.strip()
    if not stripped:
        raise AllowlistError("pattern must not be empty")
    try:
        return str(ipaddress.ip_network(stripped, strict=False))
    except ValueError:
        pass
    lowered = stripped.lower()
    if not _HOSTNAME_PATTERN.match(lowered):
        raise AllowlistError(
            f"pattern {stripped!r} must be a CIDR (e.g. 10.0.0.0/8) or an exact hostname"
        )
    return lowered


async def load_url_allowlist(session: AsyncSession, *, org_id: uuid.UUID) -> tuple[str, ...]:
    result = await session.execute(
        select(UrlAllowlist.pattern)
        .where(UrlAllowlist.org_id == org_id)
        .order_by(UrlAllowlist.pattern)
    )
    return tuple(result.scalars())


async def load_url_policy(
    session: AsyncSession, *, org_id: uuid.UUID, restricted: bool
) -> UrlPolicy:
    if not restricted:
        return UrlPolicy(restricted=False)
    return UrlPolicy(restricted=True, allowlist=await load_url_allowlist(session, org_id=org_id))


async def add_url_allowlist(
    session: AsyncSession,
    *,
    pattern: str,
    note: str | None = None,
    org_slug: str = DEFAULT_ORG_SLUG,
) -> UrlAllowlist:
    normalized = normalize_pattern(pattern)
    org = await ensure_org(session, slug=org_slug)
    existing = await session.execute(
        select(UrlAllowlist.id).where(
            UrlAllowlist.org_id == org.id, UrlAllowlist.pattern == normalized
        )
    )
    if existing.first() is not None:
        raise AllowlistError(f"pattern {normalized!r} is already allowlisted")
    row = UrlAllowlist(org_id=org.id, pattern=normalized, note=note)
    session.add(row)
    await session.flush()
    return row


async def remove_url_allowlist(
    session: AsyncSession, *, pattern: str, org_slug: str = DEFAULT_ORG_SLUG
) -> str:
    normalized = normalize_pattern(pattern)
    org = await ensure_org(session, slug=org_slug)
    existing = await session.execute(
        select(UrlAllowlist.id).where(
            UrlAllowlist.org_id == org.id, UrlAllowlist.pattern == normalized
        )
    )
    if existing.first() is None:
        raise AllowlistError(f"pattern {normalized!r} is not allowlisted")
    await session.execute(
        delete(UrlAllowlist).where(
            UrlAllowlist.org_id == org.id, UrlAllowlist.pattern == normalized
        )
    )
    await session.flush()
    return normalized
