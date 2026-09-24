"""Access resolution: the single security boundary for list, describe and invoke.

Every path that lists, describes or invokes a tool goes through these
functions. Global services are reached through admin visibility; personal
services through the owner's handle (owner access in Phase 2.2, shares from
Phase 2.3). Callers without access always get `ServiceNotFound`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.audit.writer import AuditRef
from cam.connections.tokens import AuthenticatedIdentity
from cam.db.models import Connection, Service, ServiceVisibility, Share, Tool, User

GLOBAL_OWNER = "global"

AccessState = Literal["none", "granted", "owner_deactivated"]


class ServiceNotFound(Exception):
    """Unknown ID, excluded tool, or no access."""


class ServiceDisabled(Exception):
    """The service exists and the caller may reach it, but it is not active."""

    def __init__(self, ref: str, *, audit_ref: AuditRef | None = None) -> None:
        super().__init__(ref)
        self.audit_ref = audit_ref


class OwnerDeactivated(Exception):
    """The caller holds a share, but the owner's account is deactivated."""

    def __init__(self, ref: str, *, audit_ref: AuditRef | None = None) -> None:
        super().__init__(ref)
        self.audit_ref = audit_ref


@dataclass(frozen=True, slots=True)
class ServiceSummary:
    id: str
    name: str
    description: str
    tier: str
    owner: str | None
    shared_by: str | None
    health: str
    tool_count: int


@dataclass(frozen=True, slots=True)
class ResolvedService:
    service: Service
    ref: str
    owner_handle: str | None


@dataclass(frozen=True, slots=True)
class ResolvedTool:
    id: str
    service_ref: str
    service: Service
    tool: Tool


def parse_service_ref(ref: str) -> tuple[str, str]:
    parts = ref.split("/")
    if len(parts) != 2:
        raise ServiceNotFound(ref)
    owner, slug = parts
    if not owner or not slug:
        raise ServiceNotFound(ref)
    return owner.lower(), slug


def parse_tool_id(tool_id: str) -> tuple[str, str, str]:
    parts = tool_id.split("/")
    if len(parts) != 3:
        raise ServiceNotFound(tool_id)
    owner, slug, name = parts
    if not owner or not slug or not name:
        raise ServiceNotFound(tool_id)
    return owner.lower(), slug, name


def service_ref(service: Service, owner_handle: str | None = None) -> str:
    if service.tier == "global":
        return f"{GLOBAL_OWNER}/{service.slug}"
    if not owner_handle:
        raise ValueError(f"owner handle required to build the ref for service {service.slug!r}")
    return f"{owner_handle}/{service.slug}"


def tool_ref(service: Service, tool: Tool, owner_handle: str | None = None) -> str:
    return f"{service_ref(service, owner_handle)}/{tool.name}"


async def _access_state(
    session: AsyncSession, identity: AuthenticatedIdentity, service: Service
) -> AccessState:
    if service.org_id != identity.org_id:
        return "none"
    if service.tier == "personal":
        if service.owner_user_id == identity.user.id:
            return "granted"
        result = await session.execute(
            select(Share.id)
            .join(Connection, Share.connection_id == Connection.id)
            .where(
                Share.org_id == identity.org_id,
                Connection.service_id == service.id,
                Connection.user_id == service.owner_user_id,
                Share.grantee_user_id == identity.user.id,
                Share.revoked_at.is_(None),
            )
        )
        if result.first() is None:
            return "none"
        owner = await session.get(User, service.owner_user_id)
        if owner is None or owner.status != "active" or owner.deactivated_at is not None:
            return "owner_deactivated"
        return "granted"
    if service.visibility == "all":
        return "granted"
    result = await session.execute(
        select(ServiceVisibility.id).where(
            ServiceVisibility.org_id == identity.org_id,
            ServiceVisibility.service_id == service.id,
            ServiceVisibility.user_id == identity.user.id,
        )
    )
    return "granted" if result.first() is not None else "none"


async def has_access(
    session: AsyncSession, identity: AuthenticatedIdentity, service: Service
) -> bool:
    return await _access_state(session, identity, service) == "granted"


async def _require_access(
    session: AsyncSession,
    identity: AuthenticatedIdentity,
    service: Service,
    *,
    ref: str,
    resolved_ref: str,
) -> None:
    state = await _access_state(session, identity, service)
    if state == "none":
        raise ServiceNotFound(ref)
    if state == "owner_deactivated":
        raise OwnerDeactivated(resolved_ref, audit_ref=AuditRef(service_id=service.id))
    if service.status != "active":
        raise ServiceDisabled(resolved_ref, audit_ref=AuditRef(service_id=service.id))


async def _lookup_service(
    session: AsyncSession, identity: AuthenticatedIdentity, owner: str, slug: str
) -> tuple[Service, str | None]:
    if owner == GLOBAL_OWNER:
        result = await session.execute(
            select(Service).where(
                Service.org_id == identity.org_id,
                Service.tier == GLOBAL_OWNER,
                Service.slug == slug,
            )
        )
        service = result.scalar_one_or_none()
        if service is None:
            raise ServiceNotFound(f"{GLOBAL_OWNER}/{slug}")
        return service, None
    owner_result = await session.execute(
        select(User).where(User.org_id == identity.org_id, User.handle == owner)
    )
    owner_user = owner_result.scalar_one_or_none()
    if owner_user is None:
        raise ServiceNotFound(f"{owner}/{slug}")
    result = await session.execute(
        select(Service).where(
            Service.org_id == identity.org_id,
            Service.tier == "personal",
            Service.owner_user_id == owner_user.id,
            Service.slug == slug,
        )
    )
    service = result.scalar_one_or_none()
    if service is None:
        raise ServiceNotFound(f"{owner}/{slug}")
    return service, owner_user.handle


async def list_services(
    session: AsyncSession, identity: AuthenticatedIdentity
) -> list[ServiceSummary]:
    stmt = (
        select(Service, User.handle, func.count(Tool.id))
        .outerjoin(User, User.id == Service.owner_user_id)
        .outerjoin(
            Tool,
            and_(
                Tool.service_id == Service.id,
                Tool.enabled.is_(True),
                Tool.status == "active",
            ),
        )
        .where(
            Service.org_id == identity.org_id,
            or_(Service.tier == GLOBAL_OWNER, Service.tier == "personal"),
            Service.status == "active",
        )
        .group_by(Service.id, User.handle)
        .order_by(Service.slug)
    )
    rows = (await session.execute(stmt)).all()
    allowed: list[tuple[Service, str | None, int]] = []
    for service, owner_handle, tool_count in rows:
        if await has_access(session, identity, service):
            allowed.append((service, owner_handle, tool_count))
    return [
        ServiceSummary(
            id=service_ref(service, owner_handle),
            name=service.name,
            description=service.description,
            tier=service.tier,
            owner=owner_handle if service.tier == "personal" else None,
            shared_by=(
                owner_handle
                if service.tier == "personal" and service.owner_user_id != identity.user.id
                else None
            ),
            health=service.health,
            tool_count=tool_count,
        )
        for service, owner_handle, tool_count in allowed
    ]


async def resolve_service(
    session: AsyncSession, identity: AuthenticatedIdentity, ref: str
) -> ResolvedService:
    owner, slug = parse_service_ref(ref)
    service, owner_handle = await _lookup_service(session, identity, owner, slug)
    resolved_ref = service_ref(service, owner_handle)
    await _require_access(session, identity, service, ref=ref, resolved_ref=resolved_ref)
    return ResolvedService(service=service, ref=resolved_ref, owner_handle=owner_handle)


async def list_tools(
    session: AsyncSession, identity: AuthenticatedIdentity, ref: str
) -> tuple[ResolvedService, list[Tool]]:
    resolved = await resolve_service(session, identity, ref)
    result = await session.execute(
        select(Tool)
        .where(
            Tool.service_id == resolved.service.id,
            Tool.enabled.is_(True),
            Tool.status == "active",
        )
        .order_by(Tool.name)
    )
    return resolved, list(result.scalars())


async def resolve_tool(
    session: AsyncSession, identity: AuthenticatedIdentity, tool_id: str
) -> ResolvedTool:
    owner, slug, name = parse_tool_id(tool_id)
    service, owner_handle = await _lookup_service(session, identity, owner, slug)
    resolved_ref = service_ref(service, owner_handle)
    await _require_access(session, identity, service, ref=tool_id, resolved_ref=resolved_ref)
    tool_result = await session.execute(
        select(Tool).where(Tool.service_id == service.id, Tool.name == name)
    )
    tool = tool_result.scalar_one_or_none()
    if tool is None or not tool.enabled or tool.status != "active":
        raise ServiceNotFound(tool_id)
    return ResolvedTool(
        id=tool_ref(service, tool, owner_handle),
        service_ref=resolved_ref,
        service=service,
        tool=tool,
    )


async def resolve_org_connection(session: AsyncSession, service: Service) -> Connection:
    result = await session.execute(
        select(Connection).where(
            Connection.org_id == service.org_id,
            Connection.service_id == service.id,
            Connection.user_id.is_(None),
        )
    )
    connection = result.scalar_one_or_none()
    if connection is None or connection.status != "connected":
        raise ServiceDisabled(service_ref(service), audit_ref=AuditRef(service_id=service.id))
    return connection


async def resolve_connection(session: AsyncSession, service: Service, *, ref: str) -> Connection:
    if service.tier == "global":
        try:
            return await resolve_org_connection(session, service)
        except ServiceDisabled:
            raise ServiceDisabled(ref, audit_ref=AuditRef(service_id=service.id)) from None
    if service.tier == "personal":
        result = await session.execute(
            select(Connection).where(
                Connection.org_id == service.org_id,
                Connection.service_id == service.id,
                Connection.user_id == service.owner_user_id,
            )
        )
        connection = result.scalar_one_or_none()
        if connection is None or connection.status != "connected":
            raise ServiceDisabled(ref, audit_ref=AuditRef(service_id=service.id))
        return connection
    raise ServiceDisabled(ref, audit_ref=AuditRef(service_id=service.id))
