"""Org URL allowlist rows and policy loading."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cam.connections.orgs import ensure_org
from cam.connections.url_policy import (
    AllowlistError,
    add_url_allowlist,
    load_url_allowlist,
    load_url_policy,
    normalize_pattern,
    remove_url_allowlist,
)
from cam.upstream.url import UrlPolicy


def _org_slug() -> str:
    return f"urlpol-{uuid.uuid4().hex[:10]}"


def test_normalize_pattern_canonicalizes_inputs() -> None:
    assert normalize_pattern("10.0.0.1/8") == "10.0.0.0/8"
    assert normalize_pattern("  Windmill.Internal  ") == "windmill.internal"
    assert normalize_pattern("svc.example.com") == "svc.example.com"


def test_normalize_pattern_rejects_junk() -> None:
    with pytest.raises(AllowlistError, match="must not be empty"):
        normalize_pattern("   ")
    with pytest.raises(AllowlistError, match="must be a CIDR"):
        normalize_pattern("not a pattern!")


async def test_load_url_policy_shapes(db_session: AsyncSession) -> None:
    slug = _org_slug()
    org = await ensure_org(db_session, slug=slug)
    unrestricted = await load_url_policy(db_session, org_id=org.id, restricted=False)
    assert unrestricted == UrlPolicy(restricted=False)
    empty_restricted = await load_url_policy(db_session, org_id=org.id, restricted=True)
    assert empty_restricted == UrlPolicy(restricted=True, allowlist=())

    await add_url_allowlist(db_session, pattern="10.0.0.0/8", org_slug=slug)
    await add_url_allowlist(db_session, pattern="windmill.internal", org_slug=slug)
    loaded = await load_url_policy(db_session, org_id=org.id, restricted=True)
    assert loaded.restricted is True
    assert loaded.allowlist == ("10.0.0.0/8", "windmill.internal")
    assert await load_url_allowlist(db_session, org_id=org.id) == loaded.allowlist


async def test_add_and_remove_allowlist(db_session: AsyncSession) -> None:
    slug = _org_slug()
    await ensure_org(db_session, slug=slug)
    row = await add_url_allowlist(
        db_session, pattern="svc.example.com", org_slug=slug, note="why not"
    )
    assert row.pattern == "svc.example.com"
    assert row.note == "why not"
    with pytest.raises(AllowlistError, match="already allowlisted"):
        await add_url_allowlist(db_session, pattern="SVC.example.com", org_slug=slug)

    removed = await remove_url_allowlist(db_session, pattern="svc.example.com", org_slug=slug)
    assert removed == "svc.example.com"
    with pytest.raises(AllowlistError, match="not allowlisted"):
        await remove_url_allowlist(db_session, pattern="svc.example.com", org_slug=slug)
