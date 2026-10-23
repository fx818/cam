"""R19 — unified registration must DNS-validate personal service/spec URLs.

The old personal-MCP path called `validate_outbound_url` (DNS resolution +
blocked-range check); the unified `register_service` only did the static
`admit_url` check, so a personal HTTPS URL could register while resolving to
an internal address. Registration now performs the DNS check too.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cam.accounts.users import User, create_user
from cam.connections.orgs import ensure_org
from cam.registry.services import ServiceError, register_service
from cam.upstream.url import PinnedDestination, UrlPolicyError


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _owner(db_session: AsyncSession) -> User:
    org = await ensure_org(db_session)
    return await create_user(
        db_session,
        org_id=org.id,
        email=f"r19-{uuid.uuid4().hex[:10]}@example.com",
        password="pw-whatever",
        role="user",
    )


async def test_register_service_personal_validates_url_dns(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await _owner(db_session)
    seen: list[str] = []

    async def _spy(url: str, *, policy: object, resolver: object = None) -> PinnedDestination:
        seen.append(url)
        return PinnedDestination(host="example.com", port=443, ip=None)

    monkeypatch.setattr("cam.registry.services.validate_outbound_url", _spy)
    service, _connection = await register_service(
        db_session,
        tier="personal",
        owner=owner,
        slug=_unique("r19ok"),
        name="OK",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="none",
    )
    assert service.url == "https://example.com/mcp"
    assert "https://example.com/mcp" in seen


async def test_register_service_personal_rejects_dns_blocked_url(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await _owner(db_session)

    async def _blocked(url: str, *, policy: object, resolver: object = None) -> PinnedDestination:
        raise UrlPolicyError("URL host 'sneaky.example.com' resolves to blocked address")

    monkeypatch.setattr("cam.registry.services.validate_outbound_url", _blocked)
    with pytest.raises(ServiceError, match="blocked address"):
        await register_service(
            db_session,
            tier="personal",
            owner=owner,
            slug=_unique("r19bad"),
            name="Sneaky",
            url="https://sneaky.example.com/mcp",
            transport="streamable-http",
            auth_type="none",
        )


async def test_register_service_personal_rejects_dns_blocked_spec_url(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await _owner(db_session)

    async def _blocked_spec_only(
        url: str, *, policy: object, resolver: object = None
    ) -> PinnedDestination:
        if "spec.example.com" in url:
            raise UrlPolicyError("URL host 'spec.example.com' resolves to blocked address")
        return PinnedDestination(host="api.example.com", port=443, ip=None)

    monkeypatch.setattr("cam.registry.services.validate_outbound_url", _blocked_spec_only)
    with pytest.raises(ServiceError, match="blocked address"):
        await register_service(
            db_session,
            tier="personal",
            owner=owner,
            slug=_unique("r19spec"),
            name="Spec Sneaky",
            url="https://api.example.com",
            kind="rest",
            auth_type="none",
            spec_url="https://spec.example.com/openapi.json",
        )


async def test_register_service_personal_allows_unresolvable_host(
    db_session: AsyncSession,
) -> None:
    # Real DNS: mcp.example.com is NXDOMAIN. Registration is allowed because the
    # request-time URL policy re-checks every destination; only a host that
    # *resolves* to a blocked address is rejected at registration.
    owner = await _owner(db_session)
    service, _connection = await register_service(
        db_session,
        tier="personal",
        owner=owner,
        slug=_unique("r19nx"),
        name="Unresolved",
        url="https://mcp.example.com/mcp",
        transport="streamable-http",
        auth_type="none",
    )
    assert service.url == "https://mcp.example.com/mcp"
