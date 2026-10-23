"""Access resolution: the single security boundary for list, describe and invoke.

Every path that lists, describes or invokes a tool goes through these
functions. Global services are reached through admin visibility; personal
services through the owner's handle (owner access in Phase 2.2, shares from
Phase 2.3). Callers without access always get `ServiceNotFound`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import and_, func, select
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

    def __init__(
        self, ref: str, *, audit_ref: AuditRef | None = None, message: str | None = None
    ) -> None:
        super().__init__(message or ref)
        self.audit_ref = audit_ref
        self.message = message or ref


class OwnerDeactivated(Exception):
    """The caller holds a share, but the owner's account is deactivated."""

    def __init__(self, ref: str, *, audit_ref: AuditRef | None = None) -> None:
        super().__init__(ref)
        self.audit_ref = audit_ref


@dataclass(frozen=True, slots=True)
class ServiceSummary:
    service_id: str
    id: str
    name: str
    description: str
    tier: str
    kind: str
    owner: str | None
    shared_by: str | None
    health: str
    tool_count: int
    connected: bool


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
    if service.tier in ("global", "template"):
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


async def _owner_qualified_template(
    session: AsyncSession,
    identity: AuthenticatedIdentity,
    owner_user: User,
    slug: str,
) -> Service | None:
    """Resolve `<handle>/<slug>` to a template the named owner can fund (R26).

    The template's canonical ref stays `global/<slug>`; this lets agents and
    the dashboard address a template owner-qualified when the named user holds
    the connection the caller is entitled to use (their own, or via a share).
    """
    result = await session.execute(
        select(Service).where(
            Service.org_id == identity.org_id,
            Service.tier == "template",
            Service.slug == slug,
        )
    )
    template = result.scalar_one_or_none()
    if template is None:
        return None
    if owner_user.id == identity.user.id:
        owned = await session.execute(
            select(Connection.id).where(
                Connection.org_id == identity.org_id,
                Connection.service_id == template.id,
                Connection.user_id == owner_user.id,
            )
        )
        return template if owned.first() is not None else None
    share = await session.execute(
        select(Share.id)
        .join(Connection, Share.connection_id == Connection.id)
        .where(
            Share.org_id == identity.org_id,
            Share.grantee_user_id == identity.user.id,
            Share.revoked_at.is_(None),
            Connection.org_id == identity.org_id,
            Connection.service_id == template.id,
            Connection.user_id == owner_user.id,
        )
    )
    return template if share.first() is not None else None


async def _lookup_service(
    session: AsyncSession, identity: AuthenticatedIdentity, owner: str, slug: str
) -> tuple[Service, str | None]:
    if owner == GLOBAL_OWNER:
        result = await session.execute(
            select(Service).where(
                Service.org_id == identity.org_id,
                Service.tier.in_((GLOBAL_OWNER, "template")),
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
        service = await _owner_qualified_template(session, identity, owner_user, slug)
    if service is None:
        raise ServiceNotFound(f"{owner}/{slug}")
    return service, owner_user.handle


async def _connection_status_for(
    session: AsyncSession, identity: AuthenticatedIdentity, service: Service
) -> bool:
    if service.tier == "template":
        result = await session.execute(
            select(Connection.status).where(
                Connection.org_id == identity.org_id,
                Connection.service_id == service.id,
                Connection.user_id == identity.user.id,
            )
        )
        row = result.first()
        return row is not None and row[0] == "connected"
    result = await session.execute(
        select(Connection.status).where(
            Connection.org_id == identity.org_id,
            Connection.service_id == service.id,
            Connection.user_id.is_(None),
        )
    )
    row = result.first()
    if row is not None:
        return row[0] == "connected"
    return service.auth_type == "none"


async def _template_connection_owner(
    session: AsyncSession, identity: AuthenticatedIdentity, service: Service
) -> tuple[str | None, str | None]:
    """(owner_handle, shared_by) for a template row (R26).

    Mirrors `resolve_connection`: the caller's own connection first, otherwise
    the deterministic oldest active share. `owner_handle` names the user whose
    credential will be used; `shared_by` is set when that is someone else.
    """
    own = await session.execute(
        select(Connection.status).where(
            Connection.org_id == identity.org_id,
            Connection.service_id == service.id,
            Connection.user_id == identity.user.id,
        )
    )
    own_row = own.first()
    if own_row is not None and own_row[0] == "connected":
        return identity.user.handle, None
    shared = await session.execute(
        select(Connection.user_id)
        .join(Share, Share.connection_id == Connection.id)
        .where(
            Share.org_id == identity.org_id,
            Connection.org_id == identity.org_id,
            Connection.service_id == service.id,
            Share.grantee_user_id == identity.user.id,
            Share.revoked_at.is_(None),
            Connection.status == "connected",
        )
        .order_by(Connection.created_at, Connection.id)
    )
    owner_id = shared.scalars().first()
    if owner_id is None:
        return None, None
    owner = await session.get(User, owner_id)
    if owner is None or owner.org_id != identity.org_id:
        return None, None
    return owner.handle, owner.handle


async def list_services(
    session: AsyncSession, identity: AuthenticatedIdentity, *, include_templates: bool = False
) -> list[ServiceSummary]:
    tiers = ("global", "personal", "template") if include_templates else ("global", "personal")
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
            Service.tier.in_(tiers),
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
    summaries: list[ServiceSummary] = []
    for service, owner_handle, tool_count in allowed:
        summary_owner = owner_handle if service.tier == "personal" else None
        summary_shared_by = (
            owner_handle
            if service.tier == "personal" and service.owner_user_id != identity.user.id
            else None
        )
        if service.tier == "template":
            # R26: templates carry the handle of the connection owner whose
            # credential will be used, so the gateway description can attribute
            # them (own connection, else a share).
            summary_owner, summary_shared_by = await _template_connection_owner(
                session, identity, service
            )
        summaries.append(
            ServiceSummary(
                service_id=str(service.id),
                id=service_ref(service, owner_handle),
                name=service.name,
                description=service.description,
                tier=service.tier,
                kind=service.kind,
                owner=summary_owner,
                shared_by=summary_shared_by,
                health=service.health,
                tool_count=tool_count,
                connected=await _connection_status_for(session, identity, service),
            )
        )
    return summaries


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
            Tool.org_id == resolved.service.org_id,
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
        select(Tool).where(
            Tool.org_id == identity.org_id,
            Tool.service_id == service.id,
            Tool.name == name,
        )
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


async def resolve_connection(
    session: AsyncSession, service: Service, *, ref: str, user_id: uuid.UUID | None = None
) -> Connection:
    if service.tier in ("global", "template"):
        try:
            return await resolve_org_connection(session, service)
        except ServiceDisabled:
            if service.tier != "template" or user_id is None:
                raise ServiceDisabled(ref, audit_ref=AuditRef(service_id=service.id)) from None
        result = await session.execute(
            select(Connection).where(
                Connection.org_id == service.org_id,
                Connection.service_id == service.id,
                Connection.user_id == user_id,
            )
        )
        connection = result.scalar_one_or_none()
        if connection is not None and connection.status == "connected":
            return connection
        shared = await session.execute(
            select(Connection)
            .join(Share, Share.connection_id == Connection.id)
            .where(
                Share.org_id == service.org_id,
                Connection.org_id == service.org_id,
                Connection.service_id == service.id,
                Share.grantee_user_id == user_id,
                Share.revoked_at.is_(None),
                # Pick the oldest *connected* share so the caller's resolution
                # agrees with the owner attribution in the tool description
                # (N5): filtering status in SQL avoids picking a disconnected
                # share and then failing while a connected one exists.
                Connection.status == "connected",
            )
            # R26: deterministic owner-qualified fallback (oldest share first)
            # instead of an unordered `.first()`.
            .order_by(Connection.created_at, Connection.id)
        )
        shared_connection = shared.scalars().first()
        if shared_connection is not None and shared_connection.status == "connected":
            return shared_connection
        raise ServiceDisabled(
            ref,
            audit_ref=AuditRef(service_id=service.id),
            message=(
                f"This template needs a connection. Connect your own {service.name} "
                "credentials in the CAM dashboard."
            ),
        ) from None
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
