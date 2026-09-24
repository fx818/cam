import re
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.access.resolution import ServiceNotFound, parse_service_ref
from cam.accounts.users import get_user_by_email
from cam.config import Settings
from cam.connections.crypto import MasterKeyRing, encrypt_secret, get_default_key_ring
from cam.connections.orgs import ensure_org, get_org_data_key
from cam.db.models import Connection, Service, ServiceVisibility, User
from cam.upstream.url import admit_url

McpTransport = Literal["streamable-http", "sse"]
McpAuthType = Literal["none", "static-header", "bearer"]
ServiceVisibilityLevel = Literal["all", "restricted"]
ServiceStatus = Literal["active", "disabled"]

_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_HTTP_SCHEMES = ("http://", "https://")
_VISIBILITY_LEVELS = ("all", "restricted")
_SERVICE_STATUSES = ("active", "disabled")


class ServiceError(Exception):
    pass


class ServiceAlreadyExists(ServiceError):
    pass


def _validate_registration(
    *,
    slug: str,
    name: str,
    url: str,
    transport: str,
    auth_type: str,
    header_name: str | None,
    credential: str | None,
) -> tuple[str, str, str, dict[str, str]]:
    resolved_slug = slug.strip().lower()
    if not _SLUG_PATTERN.match(resolved_slug):
        raise ServiceError(
            "slug must start with a letter or digit and contain only lowercase "
            "letters, digits, dots, underscores or hyphens"
        )
    if transport not in ("streamable-http", "sse"):
        raise ServiceError("transport must be streamable-http or sse")
    if auth_type not in ("none", "static-header", "bearer"):
        raise ServiceError("auth type must be none, static-header or bearer")
    resolved_name = name.strip()
    if not resolved_name:
        raise ServiceError("name must not be empty")
    if not url.strip().startswith(_HTTP_SCHEMES):
        raise ServiceError("url must start with http:// or https://")
    admit_url(url.strip())
    auth_config: dict[str, str] = {}
    if auth_type == "static-header":
        resolved_header_name = (header_name or "").strip()
        if not resolved_header_name:
            raise ServiceError("static-header auth requires a header name")
        if credential is None:
            raise ServiceError("static-header auth requires a header value")
        auth_config = {"header_name": resolved_header_name}
    elif auth_type == "bearer":
        if credential is None:
            raise ServiceError("bearer auth requires a token")
    elif credential is not None:
        raise ServiceError("auth type none does not take a credential")
    return resolved_slug, resolved_name, url.strip(), auth_config


def _encrypt_credential(
    credential: str | None, *, data_key: bytes, key_version: int
) -> bytes | None:
    if credential is None:
        return None
    return encrypt_secret(credential.encode("utf-8"), data_key, key_version=key_version)


async def register_global_mcp(
    session: AsyncSession,
    *,
    slug: str,
    name: str,
    url: str,
    transport: McpTransport,
    auth_type: McpAuthType,
    header_name: str | None = None,
    credential: str | None = None,
    description: str = "",
    ring: MasterKeyRing | None = None,
) -> tuple[Service, Connection]:
    resolved_slug, resolved_name, resolved_url, auth_config = _validate_registration(
        slug=slug,
        name=name,
        url=url,
        transport=transport,
        auth_type=auth_type,
        header_name=header_name,
        credential=credential,
    )

    key_ring = ring if ring is not None else get_default_key_ring()
    org = await ensure_org(session, ring=key_ring)
    existing = await session.execute(
        select(Service.id).where(
            Service.org_id == org.id,
            Service.slug == resolved_slug,
            Service.tier.in_(("global", "template")),
        )
    )
    if existing.first() is not None:
        raise ServiceAlreadyExists(
            f"service slug {resolved_slug!r} is already registered in this org"
        )

    settings = Settings()
    service = Service(
        org_id=org.id,
        tier="global",
        owner_user_id=None,
        slug=resolved_slug,
        name=resolved_name,
        description=description,
        kind="mcp",
        url=resolved_url,
        transport=transport,
        auth_type=auth_type,
        auth_config=auth_config,
        visibility="all",
        timeout_ms=settings.default_timeout_s * 1000,
        response_cap_tokens=settings.default_response_cap_tokens,
        status="active",
        health="unknown",
    )
    session.add(service)
    await session.flush()

    key_version, data_key = await get_org_data_key(session, org.id, ring=key_ring)
    connection = Connection(
        org_id=org.id,
        service_id=service.id,
        user_id=None,
        secret_enc=_encrypt_credential(credential, data_key=data_key, key_version=key_version),
        key_version=key_version,
        status="connected",
    )
    session.add(connection)
    await session.flush()
    return service, connection


async def register_personal_mcp(
    session: AsyncSession,
    *,
    owner: User,
    slug: str,
    name: str,
    url: str,
    transport: McpTransport,
    auth_type: McpAuthType,
    header_name: str | None = None,
    credential: str | None = None,
    description: str = "",
    ring: MasterKeyRing | None = None,
) -> tuple[Service, Connection]:
    resolved_slug, resolved_name, resolved_url, auth_config = _validate_registration(
        slug=slug,
        name=name,
        url=url,
        transport=transport,
        auth_type=auth_type,
        header_name=header_name,
        credential=credential,
    )
    if owner.status != "active" or owner.deactivated_at is not None:
        raise ServiceError(f"user {owner.email} is not active")

    key_ring = ring if ring is not None else get_default_key_ring()
    existing = await session.execute(
        select(Service.id).where(
            Service.org_id == owner.org_id,
            Service.tier == "personal",
            Service.owner_user_id == owner.id,
            Service.slug == resolved_slug,
        )
    )
    if existing.first() is not None:
        raise ServiceAlreadyExists(
            f"personal service slug {resolved_slug!r} is already registered for this user"
        )

    settings = Settings()
    service = Service(
        org_id=owner.org_id,
        tier="personal",
        owner_user_id=owner.id,
        slug=resolved_slug,
        name=resolved_name,
        description=description,
        kind="mcp",
        url=resolved_url,
        transport=transport,
        auth_type=auth_type,
        auth_config=auth_config,
        visibility="all",
        timeout_ms=settings.default_timeout_s * 1000,
        response_cap_tokens=settings.default_response_cap_tokens,
        status="active",
        health="unknown",
    )
    session.add(service)
    await session.flush()

    key_version, data_key = await get_org_data_key(session, owner.org_id, ring=key_ring)
    connection = Connection(
        org_id=owner.org_id,
        service_id=service.id,
        user_id=owner.id,
        secret_enc=_encrypt_credential(credential, data_key=data_key, key_version=key_version),
        key_version=key_version,
        status="connected",
    )
    session.add(connection)
    await session.flush()
    return service, connection


async def set_service_status(session: AsyncSession, *, ref: str, status: ServiceStatus) -> Service:
    if status not in _SERVICE_STATUSES:
        raise ServiceError("status must be active or disabled")
    try:
        owner_part, slug = parse_service_ref(ref)
    except ServiceNotFound:
        raise ServiceError(
            f"invalid service id {ref!r}; expected global/<slug> or <handle>/<slug>"
        ) from None
    org = await ensure_org(session)
    if owner_part == "global":
        result = await session.execute(
            select(Service).where(
                Service.org_id == org.id,
                Service.tier == "global",
                Service.slug == slug,
            )
        )
    else:
        owner_result = await session.execute(
            select(User).where(User.org_id == org.id, User.handle == owner_part)
        )
        owner_user = owner_result.scalar_one_or_none()
        if owner_user is None:
            raise ServiceError(f"no user with handle {owner_part!r} in this org")
        result = await session.execute(
            select(Service).where(
                Service.org_id == org.id,
                Service.tier == "personal",
                Service.owner_user_id == owner_user.id,
                Service.slug == slug,
            )
        )
    service = result.scalar_one_or_none()
    if service is None:
        raise ServiceError(f"no service with id {ref!r}")
    service.status = status
    await session.flush()
    return service


async def _get_admin_service(session: AsyncSession, slug: str) -> Service:
    resolved_slug = slug.strip().lower()
    if not resolved_slug:
        raise ServiceError("slug must not be empty")
    org = await ensure_org(session)
    result = await session.execute(
        select(Service).where(
            Service.org_id == org.id,
            Service.tier.in_(("global", "template")),
            Service.slug == resolved_slug,
        )
    )
    service = result.scalar_one_or_none()
    if service is None:
        raise ServiceError(f"no global or template service with slug {resolved_slug!r}")
    return service


async def set_service_visibility(
    session: AsyncSession, *, slug: str, visibility: ServiceVisibilityLevel
) -> Service:
    if visibility not in _VISIBILITY_LEVELS:
        raise ServiceError("visibility must be all or restricted")
    service = await _get_admin_service(session, slug)
    service.visibility = visibility
    await session.flush()
    return service


async def grant_service_visibility(session: AsyncSession, *, slug: str, user_email: str) -> Service:
    service = await _get_admin_service(session, slug)
    user = await get_user_by_email(session, user_email)
    if user is None:
        raise ServiceError(f"no user with email {user_email}")
    if user.org_id != service.org_id:
        raise ServiceError("user belongs to a different org than the service")
    existing = await session.execute(
        select(ServiceVisibility.id).where(
            ServiceVisibility.org_id == service.org_id,
            ServiceVisibility.service_id == service.id,
            ServiceVisibility.user_id == user.id,
        )
    )
    if existing.first() is not None:
        raise ServiceError(f"user {user_email} already has visibility on {slug!r}")
    session.add(
        ServiceVisibility(
            org_id=service.org_id,
            service_id=service.id,
            user_id=user.id,
        )
    )
    await session.flush()
    return service


async def revoke_service_visibility(
    session: AsyncSession, *, slug: str, user_email: str
) -> Service:
    service = await _get_admin_service(session, slug)
    user = await get_user_by_email(session, user_email)
    if user is None:
        raise ServiceError(f"no user with email {user_email}")
    existing = await session.execute(
        select(ServiceVisibility.id).where(
            ServiceVisibility.org_id == service.org_id,
            ServiceVisibility.service_id == service.id,
            ServiceVisibility.user_id == user.id,
        )
    )
    if existing.first() is None:
        raise ServiceError(f"user {user_email} has no visibility grant on {slug!r}")
    await session.execute(
        delete(ServiceVisibility).where(
            ServiceVisibility.org_id == service.org_id,
            ServiceVisibility.service_id == service.id,
            ServiceVisibility.user_id == user.id,
        )
    )
    await session.flush()
    return service
