"""Access resolution: the single security boundary for list, describe and invoke.

Every path that lists, describes or invokes a tool goes through these
functions. Phase 1 scope: global services only; template and personal tiers
arrive in later phases and must be added here, not at call sites.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.audit.writer import AuditRef
from cam.connections.tokens import AuthenticatedIdentity
from cam.db.models import Connection, Service, ServiceVisibility, Tool

GLOBAL_OWNER = "global"


class ServiceNotFound(Exception):
    """Unknown ID, excluded tool, non-global owner in Phase 1, or no access."""


class ServiceDisabled(Exception):
    """The service exists and the caller may reach it, but it is not active."""

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
class ResolvedTool:
    id: str
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


def service_ref(service: Service) -> str:
    return f"{GLOBAL_OWNER}/{service.slug}"


def tool_ref(service: Service, tool: Tool) -> str:
    return f"{GLOBAL_OWNER}/{service.slug}/{tool.name}"


async def has_access(
    session: AsyncSession, identity: AuthenticatedIdentity, service: Service
) -> bool:
    if service.org_id != identity.org_id:
        return False
    if service.tier == "personal":
        return service.owner_user_id == identity.user.id
    if service.visibility == "all":
        return True
    result = await session.execute(
        select(ServiceVisibility.id).where(
            ServiceVisibility.org_id == identity.org_id,
            ServiceVisibility.service_id == service.id,
            ServiceVisibility.user_id == identity.user.id,
        )
    )
    return result.first() is not None


async def list_services(
    session: AsyncSession, identity: AuthenticatedIdentity
) -> list[ServiceSummary]:
    stmt = (
        select(Service, func.count(Tool.id))
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
            Service.tier == GLOBAL_OWNER,
            Service.status == "active",
        )
        .group_by(Service.id)
        .order_by(Service.slug)
    )
    rows = (await session.execute(stmt)).all()
    allowed: list[tuple[Service, int]] = []
    for service, tool_count in rows:
        if await has_access(session, identity, service):
            allowed.append((service, tool_count))
    return [
        ServiceSummary(
            id=service_ref(service),
            name=service.name,
            description=service.description,
            tier=service.tier,
            owner=None,
            shared_by=None,
            health=service.health,
            tool_count=tool_count,
        )
        for service, tool_count in allowed
    ]


async def resolve_service(
    session: AsyncSession, identity: AuthenticatedIdentity, ref: str
) -> Service:
    owner, slug = parse_service_ref(ref)
    if owner != GLOBAL_OWNER:
        raise ServiceNotFound(ref)
    result = await session.execute(
        select(Service).where(
            Service.org_id == identity.org_id,
            Service.tier == GLOBAL_OWNER,
            Service.slug == slug,
        )
    )
    service = result.scalar_one_or_none()
    if service is None:
        raise ServiceNotFound(ref)
    if not await has_access(session, identity, service):
        raise ServiceNotFound(ref)
    if service.status != "active":
        raise ServiceDisabled(ref, audit_ref=AuditRef(service_id=service.id))
    return service


async def list_tools(
    session: AsyncSession, identity: AuthenticatedIdentity, ref: str
) -> tuple[Service, list[Tool]]:
    service = await resolve_service(session, identity, ref)
    result = await session.execute(
        select(Tool)
        .where(
            Tool.service_id == service.id,
            Tool.enabled.is_(True),
            Tool.status == "active",
        )
        .order_by(Tool.name)
    )
    return service, list(result.scalars())


async def resolve_tool(
    session: AsyncSession, identity: AuthenticatedIdentity, tool_id: str
) -> ResolvedTool:
    owner, slug, name = parse_tool_id(tool_id)
    if owner != GLOBAL_OWNER:
        raise ServiceNotFound(tool_id)
    result = await session.execute(
        select(Service).where(
            Service.org_id == identity.org_id,
            Service.tier == GLOBAL_OWNER,
            Service.slug == slug,
        )
    )
    service = result.scalar_one_or_none()
    if service is None:
        raise ServiceNotFound(tool_id)
    if not await has_access(session, identity, service):
        raise ServiceNotFound(tool_id)
    if service.status != "active":
        raise ServiceDisabled(tool_id, audit_ref=AuditRef(service_id=service.id))
    tool_result = await session.execute(
        select(Tool).where(Tool.service_id == service.id, Tool.name == name)
    )
    tool = tool_result.scalar_one_or_none()
    if tool is None or not tool.enabled or tool.status != "active":
        raise ServiceNotFound(tool_id)
    return ResolvedTool(id=tool_ref(service, tool), service=service, tool=tool)


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
