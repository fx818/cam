from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

from sqlalchemy import String, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cam.access.resolution import GLOBAL_OWNER, ServiceNotFound, has_access, list_services
from cam.access.shares import ShareError, add_share, list_shares, revoke_share
from cam.accounts.users import UserError, get_user_by_email
from cam.api.deps import SessionIdentity
from cam.api.pagination import decode_cursor, encode_cursor
from cam.connections.tokens import AuthenticatedIdentity, create_token
from cam.connections.url_policy import AllowlistError, normalize_pattern
from cam.db.models import (
    AuditLog,
    CamToken,
    Connection,
    Org,
    Service,
    ServiceVisibility,
    Share,
    Tool,
    UrlAllowlist,
    User,
)

ServiceGroup = Literal["global", "templates", "mine", "shared"]
AuditScope = Literal["mine", "my_services", "both", "all"]


@dataclass(frozen=True, slots=True)
class ServiceContext:
    service: Service
    ref: str
    owner_handle: str | None
    is_owner: bool
    is_admin: bool
    can_manage: bool


def dashboard_viewer_identity(identity: SessionIdentity) -> AuthenticatedIdentity:
    return AuthenticatedIdentity(
        org_id=identity.org_id,
        user=identity.user,
        token=CamToken(
            org_id=identity.org_id,
            user_id=identity.user.id,
            label="dashboard",
            token_hash="dashboard-session",
            token_prefix="dashboard",
        ),
    )


def dashboard_ref(service: Service, owner_handle: str | None) -> str:
    if service.tier == "personal":
        if not owner_handle:
            raise ValueError(f"owner handle required for service {service.slug!r}")
        return f"{owner_handle}/{service.slug}"
    return f"{GLOBAL_OWNER}/{service.slug}"


def service_public(service: Service, owner_handle: str | None) -> dict[str, Any]:
    return {
        "id": dashboard_ref(service, owner_handle),
        "tier": service.tier,
        "name": service.name,
        "description": service.description,
        "slug": service.slug,
        "kind": service.kind,
        "url": service.url,
        "transport": service.transport,
        "spec_url": service.spec_url,
        "auth_type": service.auth_type,
        "auth_config": service.auth_config,
        "visibility": service.visibility,
        "timeout_ms": service.timeout_ms,
        "response_cap_tokens": service.response_cap_tokens,
        "status": service.status,
        "health": service.health,
        "last_synced_at": service.last_synced_at.isoformat() if service.last_synced_at else None,
        "created_at": service.created_at.isoformat() if service.created_at else None,
        "owner": owner_handle,
    }


def connection_public(connection: Connection | None) -> dict[str, Any] | None:
    if connection is None:
        return None
    return {
        "id": str(connection.id),
        "status": connection.status,
        "expires_at": connection.expires_at.isoformat() if connection.expires_at else None,
        "last_rotated_at": (
            connection.last_rotated_at.isoformat() if connection.last_rotated_at else None
        ),
        "created_at": connection.created_at.isoformat() if connection.created_at else None,
    }


def token_public(token: CamToken) -> dict[str, Any]:
    return {
        "id": str(token.id),
        "label": token.label,
        "token_prefix": token.token_prefix,
        "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
        "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
        "created_at": token.created_at.isoformat() if token.created_at else None,
    }


def share_public(share: Share, grantee: User | None) -> dict[str, Any]:
    return {
        "id": str(share.id),
        "created_at": share.created_at.isoformat() if share.created_at else None,
        "revoked_at": share.revoked_at.isoformat() if share.revoked_at else None,
        "grantee": {
            "id": str(share.grantee_user_id),
            "email": grantee.email if grantee else None,
            "display_name": grantee.display_name if grantee else None,
        },
    }


def tool_public(tool: Tool) -> dict[str, Any]:
    return {
        "id": str(tool.id),
        "name": tool.name,
        "upstream_name": tool.upstream_name,
        "description": tool.description,
        "description_override": tool.description_override,
        "usage_note": tool.usage_note,
        "example_invocation": tool.example_invocation,
        "input_schema": tool.input_schema,
        "tags": tool.tags,
        "enabled": tool.enabled,
        "status": tool.status,
    }


async def _owner_handle(session: AsyncSession, service: Service) -> str | None:
    if service.owner_user_id is None:
        return None
    owner = await session.get(User, service.owner_user_id)
    return owner.handle if owner else None


async def _connection_map(
    session: AsyncSession, identity: SessionIdentity
) -> dict[tuple[str, str], Connection]:
    rows = (
        await session.execute(
            select(Service.tier, Service.slug, Connection)
            .join(Connection, Connection.service_id == Service.id)
            .where(
                Connection.org_id == identity.org_id,
                Connection.user_id == identity.user.id,
            )
        )
    ).all()
    mapping = {(tier, slug): connection for tier, slug, connection in rows}
    org_rows = (
        await session.execute(
            select(Service.slug, Connection)
            .join(Connection, Connection.service_id == Service.id)
            .where(
                Connection.org_id == identity.org_id,
                Connection.user_id.is_(None),
                Service.tier == "global",
            )
        )
    ).all()
    for slug, connection in org_rows:
        mapping[("global", slug)] = connection
    return mapping


async def list_dashboard_services(
    session: AsyncSession, identity: SessionIdentity
) -> list[dict[str, Any]]:
    viewer = dashboard_viewer_identity(identity)
    connections = await _connection_map(session, identity)
    entries: list[dict[str, Any]] = []
    for summary in await list_services(session, viewer):
        if summary.tier == "global":
            group: ServiceGroup = "global"
            connection = connections.get(("global", summary.id.split("/", 1)[1]))
        elif summary.shared_by:
            group = "shared"
            connection = None
        else:
            group = "mine"
            connection = connections.get(("personal", summary.id.split("/", 1)[1]))
        entries.append(
            {
                "id": summary.id,
                "group": group,
                "tier": summary.tier,
                "name": summary.name,
                "description": summary.description,
                "owner": summary.owner,
                "shared_by": summary.shared_by,
                "health": summary.health,
                "tool_count": summary.tool_count,
                "status": "active",
                "connection": connection_public(connection),
            }
        )

    template_rows = (
        await session.execute(
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
                Service.tier == "template",
                Service.status == "active",
            )
            .group_by(Service.id)
            .order_by(Service.slug)
        )
    ).all()
    for service, tool_count in template_rows:
        if service.visibility != "all":
            grant = await session.execute(
                select(ServiceVisibility.id).where(
                    ServiceVisibility.org_id == identity.org_id,
                    ServiceVisibility.service_id == service.id,
                    ServiceVisibility.user_id == identity.user.id,
                )
            )
            if grant.first() is None:
                continue
        connection = connections.get(("template", service.slug))
        entries.append(
            {
                "id": f"{GLOBAL_OWNER}/{service.slug}",
                "group": "templates",
                "tier": "template",
                "name": service.name,
                "description": service.description,
                "owner": None,
                "shared_by": None,
                "health": service.health,
                "tool_count": tool_count,
                "status": service.status,
                "connection": connection_public(connection),
            }
        )
    order = {"global": 0, "templates": 1, "mine": 2, "shared": 3}
    entries.sort(key=lambda entry: (order[entry["group"]], entry["name"].lower()))
    return entries


async def lookup_service_context(
    session: AsyncSession, identity: SessionIdentity, owner: str, slug: str
) -> ServiceContext:
    owner_part = owner.strip().lower()
    resolved_slug = slug.strip()
    service: Service | None = None
    owner_handle: str | None = None
    if owner_part == GLOBAL_OWNER:
        result = await session.execute(
            select(Service).where(
                Service.org_id == identity.org_id,
                Service.tier.in_(("global", "template")),
                Service.slug == resolved_slug,
            )
        )
        service = result.scalar_one_or_none()
    else:
        owner_result = await session.execute(
            select(User).where(User.org_id == identity.org_id, User.handle == owner_part)
        )
        owner_user = owner_result.scalar_one_or_none()
        if owner_user is not None:
            owner_handle = owner_user.handle
            result = await session.execute(
                select(Service).where(
                    Service.org_id == identity.org_id,
                    Service.tier == "personal",
                    Service.owner_user_id == owner_user.id,
                    Service.slug == resolved_slug,
                )
            )
            service = result.scalar_one_or_none()
    ref = f"{owner_part}/{resolved_slug}"
    if service is None or service.status == "deleted":
        raise ServiceNotFound(ref)
    if not await has_access(session, dashboard_viewer_identity(identity), service):
        raise ServiceNotFound(ref)
    resolved_handle = (
        owner_handle if owner_handle is not None else await _owner_handle(session, service)
    )
    is_owner = service.owner_user_id == identity.user.id
    is_admin = identity.user.role == "admin"
    return ServiceContext(
        service=service,
        ref=dashboard_ref(service, resolved_handle),
        owner_handle=resolved_handle,
        is_owner=is_owner,
        is_admin=is_admin,
        can_manage=is_owner or is_admin,
    )


async def viewer_connection(
    session: AsyncSession, service: Service, identity: SessionIdentity
) -> Connection | None:
    if service.tier == "personal":
        user_id: uuid.UUID | None = service.owner_user_id
    elif service.tier == "template":
        user_id = identity.user.id
    else:
        user_id = None
    result = await session.execute(
        select(Connection).where(
            Connection.org_id == identity.org_id,
            Connection.service_id == service.id,
            Connection.user_id.is_(None) if user_id is None else Connection.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


def apply_service_patch(service: Service, updates: dict[str, Any]) -> Service:
    if "name" in updates:
        service.name = str(updates["name"]).strip()
    if "description" in updates:
        service.description = str(updates["description"])
    if "timeout_ms" in updates:
        service.timeout_ms = int(updates["timeout_ms"])
    if "response_cap_tokens" in updates:
        service.response_cap_tokens = int(updates["response_cap_tokens"])
    if "status" in updates:
        status = str(updates["status"])
        if status not in ("active", "disabled"):
            raise ValueError("status must be active or disabled")
        service.status = status
    return service


async def soft_delete_service(session: AsyncSession, service: Service) -> None:
    now = datetime.now(UTC)
    service.status = "deleted"
    connections = list(
        (
            await session.execute(select(Connection).where(Connection.service_id == service.id))
        ).scalars()
    )
    for connection in connections:
        connection.status = "disabled"
        connection.secret_enc = None
        connection.refresh_enc = None
        connection.expires_at = None
        await session.execute(
            update(Share)
            .where(Share.connection_id == connection.id, Share.revoked_at.is_(None))
            .values(revoked_at=now)
        )
    await session.flush()


async def list_service_tools(
    session: AsyncSession, service: Service, *, can_manage: bool
) -> list[Tool]:
    stmt = select(Tool).where(Tool.service_id == service.id)
    if not can_manage:
        stmt = stmt.where(Tool.enabled.is_(True), Tool.status == "active")
    stmt = stmt.order_by(Tool.name)
    return list((await session.execute(stmt)).scalars())


async def get_tool_context(
    session: AsyncSession, identity: SessionIdentity, tool_id: uuid.UUID
) -> tuple[Tool, ServiceContext]:
    tool = await session.get(Tool, tool_id)
    if tool is None or tool.org_id != identity.org_id:
        raise ServiceNotFound(str(tool_id))
    service = await session.get(Service, tool.service_id)
    if service is None or service.status == "deleted":
        raise ServiceNotFound(str(tool_id))
    owner_handle = await _owner_handle(session, service)
    ref = dashboard_ref(service, owner_handle)
    if not await has_access(session, dashboard_viewer_identity(identity), service):
        raise ServiceNotFound(ref)
    is_owner = service.owner_user_id == identity.user.id
    is_admin = identity.user.role == "admin"
    return tool, ServiceContext(
        service=service,
        ref=ref,
        owner_handle=owner_handle,
        is_owner=is_owner,
        is_admin=is_admin,
        can_manage=is_owner or is_admin,
    )


def apply_tool_patch(tool: Tool, updates: dict[str, Any]) -> Tool:
    if "enabled" in updates:
        tool.enabled = bool(updates["enabled"])
    if "description_override" in updates:
        raw = updates["description_override"]
        tool.description_override = None if raw is None or raw == "" else str(raw)
    if "usage_note" in updates:
        raw = updates["usage_note"]
        tool.usage_note = None if raw is None or raw == "" else str(raw)
    if "example_invocation" in updates:
        raw = updates["example_invocation"]
        tool.example_invocation = None if raw is None else dict(raw)
    if "tags" in updates:
        raw_tags = updates["tags"]
        tool.tags = None if raw_tags is None else [str(tag) for tag in raw_tags]
    return tool


async def list_user_tokens(
    session: AsyncSession, identity: SessionIdentity, *, limit: int, cursor: str | None
) -> tuple[list[CamToken], str | None]:
    stmt = (
        select(CamToken)
        .where(CamToken.org_id == identity.org_id, CamToken.user_id == identity.user.id)
        .order_by(CamToken.created_at.desc(), CamToken.id.desc())
    )
    if cursor:
        parts = decode_cursor(cursor, count=2)
        assert parts is not None
        created = datetime.fromisoformat(parts[0])
        token_id = uuid.UUID(parts[1])
        stmt = stmt.where(
            or_(
                CamToken.created_at < created,
                and_(CamToken.created_at == created, CamToken.id < token_id),
            )
        )
    rows = list((await session.execute(stmt.limit(limit + 1))).scalars())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(last.created_at.isoformat(), str(last.id))
    return rows, next_cursor


async def create_user_token(
    session: AsyncSession, identity: SessionIdentity, *, label: str
) -> tuple[str, CamToken]:
    return await create_token(
        session, org_id=identity.org_id, user_id=identity.user.id, label=label
    )


async def revoke_user_token(
    session: AsyncSession, identity: SessionIdentity, token_id: uuid.UUID
) -> CamToken:
    token = await session.get(CamToken, token_id)
    if token is None or token.org_id != identity.org_id or token.user_id != identity.user.id:
        raise ServiceNotFound(str(token_id))
    if token.revoked_at is None:
        token.revoked_at = datetime.now(UTC)
        await session.flush()
    return token


async def get_owned_connection(
    session: AsyncSession, identity: SessionIdentity, connection_id: uuid.UUID
) -> Connection:
    connection = await session.get(Connection, connection_id)
    if connection is None or connection.org_id != identity.org_id:
        raise ServiceNotFound(str(connection_id))
    if connection.user_id is None:
        if identity.user.role != "admin":
            raise ServiceNotFound(str(connection_id))
        return connection
    if connection.user_id != identity.user.id and identity.user.role != "admin":
        raise ServiceNotFound(str(connection_id))
    return connection


async def list_connection_shares(
    session: AsyncSession, identity: SessionIdentity, connection: Connection
) -> list[dict[str, Any]]:
    if connection.user_id != identity.user.id:
        raise ShareError("connection not found")
    shares = await list_shares(session, connection_id=connection.id, actor=identity.user)
    grantee_ids = {share.grantee_user_id for share in shares}
    users: dict[uuid.UUID, User] = {}
    if grantee_ids:
        rows = await session.execute(select(User).where(User.id.in_(grantee_ids)))
        users = {user.id: user for user in rows.scalars()}
    return [share_public(share, users.get(share.grantee_user_id)) for share in shares]


async def add_connection_share(
    session: AsyncSession, identity: SessionIdentity, connection: Connection, *, email: str
) -> Share:
    if connection.user_id != identity.user.id:
        raise ShareError("connection not found")
    try:
        grantee = await get_user_by_email(session, email)
    except UserError as exc:
        raise ShareError(str(exc)) from exc
    if grantee is None:
        raise ShareError(f"no user with email {email}")
    return await add_share(
        session, connection_id=connection.id, grantee_user_id=grantee.id, actor=identity.user
    )


async def revoke_share_by_id(
    session: AsyncSession, identity: SessionIdentity, share_id: uuid.UUID
) -> Share:
    return await revoke_share(session, share_id=share_id, actor=identity.user)


@dataclass(frozen=True, slots=True)
class AuditFilters:
    scope: AuditScope
    agent: str | None
    service_ref: str | None
    status: str | None
    date_from: date | None
    date_to: date | None
    agent_name: str | None = None
    token_id: uuid.UUID | None = None
    args_query: str | None = None


def apply_audit_filters(stmt: Any, filters: AuditFilters) -> Any:
    """Admin/forensics filters: exact token, agent name and args text search."""
    if filters.token_id is not None:
        stmt = stmt.where(AuditLog.token_id == filters.token_id)
    if filters.agent_name:
        stmt = stmt.where(AuditLog.agent_name == filters.agent_name)
    if filters.args_query:
        needle = str(filters.args_query)
        stmt = stmt.where(AuditLog.args.cast(String).ilike(f"%{needle}%"))
    return stmt


async def _resolve_audit_service(
    session: AsyncSession, identity: SessionIdentity, ref: str
) -> uuid.UUID | None:
    parts = ref.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    owner_part, slug = parts
    if owner_part == GLOBAL_OWNER:
        result = await session.execute(
            select(Service.id).where(
                Service.org_id == identity.org_id,
                Service.tier.in_(("global", "template")),
                Service.slug == slug,
            )
        )
        return result.scalar_one_or_none()
    owner_result = await session.execute(
        select(User.id).where(User.org_id == identity.org_id, User.handle == owner_part)
    )
    owner_id = owner_result.scalar_one_or_none()
    if owner_id is None:
        return None
    result = await session.execute(
        select(Service.id).where(
            Service.org_id == identity.org_id,
            Service.tier == "personal",
            Service.owner_user_id == owner_id,
            Service.slug == slug,
        )
    )
    return result.scalar_one_or_none()


async def _hydrate_audit_entries(
    session: AsyncSession, rows: list[AuditLog], *, include_args: bool = False
) -> list[dict[str, Any]]:
    token_ids = {row.token_id for row in rows if row.token_id is not None}
    user_ids = {row.user_id for row in rows if row.user_id is not None}
    service_ids = {row.service_id for row in rows if row.service_id is not None}
    tokens: dict[uuid.UUID, CamToken] = {}
    users: dict[uuid.UUID, User] = {}
    services: dict[uuid.UUID, Service] = {}
    owners: dict[uuid.UUID, User] = {}
    if token_ids:
        tokens = {
            token.id: token
            for token in (
                await session.execute(select(CamToken).where(CamToken.id.in_(token_ids)))
            ).scalars()
        }
    if user_ids:
        users = {
            user.id: user
            for user in (await session.execute(select(User).where(User.id.in_(user_ids)))).scalars()
        }
    if service_ids:
        services = {
            service.id: service
            for service in (
                await session.execute(select(Service).where(Service.id.in_(service_ids)))
            ).scalars()
        }
        owner_ids = {s.owner_user_id for s in services.values() if s.owner_user_id is not None}
        if owner_ids:
            owners = {
                user.id: user
                for user in (
                    await session.execute(select(User).where(User.id.in_(owner_ids)))
                ).scalars()
            }
    entries: list[dict[str, Any]] = []
    for row in rows:
        service_ref = None
        if row.service_id is not None and row.service_id in services:
            service = services[row.service_id]
            owner_handle = None
            if service.owner_user_id is not None and service.owner_user_id in owners:
                owner_handle = owners[service.owner_user_id].handle
            try:
                service_ref = dashboard_ref(service, owner_handle)
            except ValueError:
                service_ref = None
        actor = users.get(row.user_id) if row.user_id else None
        token = tokens.get(row.token_id) if row.token_id else None
        entry_dict: dict[str, Any] = {
            "id": str(row.id),
            "ts": row.ts.isoformat() if row.ts else None,
            "action": row.action,
            "agent_name": row.agent_name,
            "agent_version": row.agent_version,
            "tool_name": row.tool_name,
            "status": row.status,
            "error_code": row.error_code,
            "error_message": row.error_message,
            "latency_ms": row.latency_ms,
            "response_bytes": row.response_bytes,
            "truncated": row.truncated,
            "service_ref": service_ref,
            "actor_email": actor.email if actor else None,
            "token_label": token.label if token else None,
        }
        if include_args:
            entry_dict["args"] = row.args
        entries.append(entry_dict)
    return entries


async def list_audit_entries(
    session: AsyncSession,
    identity: SessionIdentity,
    filters: AuditFilters,
    *,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    stmt = select(AuditLog).where(AuditLog.org_id == identity.org_id)
    if filters.scope == "all":
        pass  # admin forensics: org-wide
    elif filters.scope == "mine":
        stmt = stmt.where(AuditLog.user_id == identity.user.id)
    elif filters.scope == "my_services":
        stmt = stmt.where(AuditLog.owner_user_id == identity.user.id)
    else:
        stmt = stmt.where(
            or_(
                AuditLog.user_id == identity.user.id,
                AuditLog.owner_user_id == identity.user.id,
            )
        )
    if filters.agent:
        stmt = stmt.where(AuditLog.agent_name.ilike(f"%{filters.agent}%"))
    if filters.status:
        stmt = stmt.where(AuditLog.status == filters.status)
    if filters.service_ref:
        service_id = await _resolve_audit_service(session, identity, filters.service_ref)
        if service_id is None:
            return [], None
        stmt = stmt.where(AuditLog.service_id == service_id)
    if filters.date_from:
        start = datetime.combine(filters.date_from, time.min, tzinfo=UTC)
        stmt = stmt.where(AuditLog.ts >= start)
    if filters.date_to:
        end = datetime.combine(filters.date_to + timedelta(days=1), time.min, tzinfo=UTC)
        stmt = stmt.where(AuditLog.ts < end)
    stmt = apply_audit_filters(stmt, filters)
    if cursor:
        parts = decode_cursor(cursor, count=2)
        assert parts is not None
        ts = datetime.fromisoformat(parts[0])
        entry_id = uuid.UUID(parts[1])
        stmt = stmt.where(
            or_(
                AuditLog.ts < ts,
                and_(AuditLog.ts == ts, AuditLog.id < entry_id),
            )
        )
    stmt = stmt.order_by(AuditLog.ts.desc(), AuditLog.id.desc()).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars().all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        assert last.ts is not None
        next_cursor = encode_cursor(last.ts.isoformat(), str(last.id))
    return await _hydrate_audit_entries(session, rows), next_cursor


def audit_row_for_export(entry: dict[str, Any]) -> dict[str, Any]:
    """Flatten one hydrated audit entry for CSV export (full args included)."""
    return {
        "ts": entry.get("ts"),
        "action": entry.get("action"),
        "status": entry.get("status"),
        "error_code": entry.get("error_code"),
        "actor_email": entry.get("actor_email"),
        "token_label": entry.get("token_label"),
        "agent_name": entry.get("agent_name"),
        "agent_version": entry.get("agent_version"),
        "service_ref": entry.get("service_ref"),
        "tool_name": entry.get("tool_name"),
        "latency_ms": entry.get("latency_ms"),
        "response_bytes": entry.get("response_bytes"),
        "truncated": entry.get("truncated"),
        "args": entry.get("args"),
    }


_EXPORT_COLUMNS = (
    "ts",
    "action",
    "status",
    "error_code",
    "actor_email",
    "token_label",
    "agent_name",
    "agent_version",
    "service_ref",
    "tool_name",
    "latency_ms",
    "response_bytes",
    "truncated",
    "args",
)


def build_audit_csv(entries: list[dict[str, Any]]) -> tuple[str, str]:
    """RFC-4180 CSV of flattened audit entries; returns (csv_text, filename)."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_EXPORT_COLUMNS, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for entry in entries:
        row = audit_row_for_export(entry)
        row["args"] = json.dumps(row["args"], sort_keys=True) if row["args"] is not None else ""
        writer.writerow({column: row.get(column) or "" for column in _EXPORT_COLUMNS})
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return buffer.getvalue(), f"cam-audit-{stamp}.csv"


async def admin_forensic_export(
    session: AsyncSession,
    identity: SessionIdentity,
    filters: AuditFilters,
    *,
    max_rows: int = 5000,
) -> list[dict[str, Any]]:
    """Org-wide audit read with the forensics filters applied, capped."""
    stmt = select(AuditLog).where(AuditLog.org_id == identity.org_id)
    stmt = apply_audit_filters(stmt, filters)
    if filters.status:
        stmt = stmt.where(AuditLog.status == filters.status)
    if filters.service_ref:
        service_id = await _resolve_audit_service(session, identity, filters.service_ref)
        if service_id is None:
            return []
        stmt = stmt.where(AuditLog.service_id == service_id)
    if filters.date_from:
        start = datetime.combine(filters.date_from, time.min, tzinfo=UTC)
        stmt = stmt.where(AuditLog.ts >= start)
    if filters.date_to:
        end = datetime.combine(filters.date_to + timedelta(days=1), time.min, tzinfo=UTC)
        stmt = stmt.where(AuditLog.ts < end)
    stmt = stmt.order_by(AuditLog.ts.desc(), AuditLog.id.desc()).limit(max_rows)
    rows = list((await session.execute(stmt)).scalars().all())
    return await _hydrate_audit_entries(session, rows, include_args=True)


async def get_audit_entry(
    session: AsyncSession, identity: SessionIdentity, entry_id: uuid.UUID
) -> dict[str, Any]:
    row = (
        await session.execute(select(AuditLog).where(AuditLog.id == entry_id))
    ).scalar_one_or_none()
    if row is None or row.org_id != identity.org_id:
        raise ServiceNotFound(str(entry_id))
    is_admin = identity.user.role == "admin"
    if not is_admin and row.user_id != identity.user.id and row.owner_user_id != identity.user.id:
        raise ServiceNotFound(str(entry_id))
    entries = await _hydrate_audit_entries(session, [row])
    entry = entries[0]
    entry["args"] = row.args
    return entry


def admin_user_public(user: User) -> dict[str, Any]:
    payload = {
        "id": str(user.id),
        "email": user.email,
        "handle": user.handle,
        "display_name": user.display_name,
        "role": user.role,
        "kind": user.kind,
        "status": user.status,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "deactivated_at": user.deactivated_at.isoformat() if user.deactivated_at else None,
    }
    return payload


async def list_org_users(session: AsyncSession, identity: SessionIdentity) -> list[dict[str, Any]]:
    result = await session.execute(
        select(User).where(User.org_id == identity.org_id).order_by(User.email)
    )
    return [admin_user_public(user) for user in result.scalars()]


async def get_org_user(
    session: AsyncSession, identity: SessionIdentity, user_id: uuid.UUID
) -> User:
    user = await session.get(User, user_id)
    if user is None or user.org_id != identity.org_id:
        raise ServiceNotFound(str(user_id))
    return user


async def count_active_admins(
    session: AsyncSession, identity: SessionIdentity, *, exclude: uuid.UUID | None = None
) -> int:
    stmt = select(func.count(User.id)).where(
        User.org_id == identity.org_id,
        User.role == "admin",
        User.status == "active",
    )
    if exclude is not None:
        stmt = stmt.where(User.id != exclude)
    return int((await session.execute(stmt)).scalar_one())


async def list_tokens_for_user(
    session: AsyncSession, identity: SessionIdentity, target: User
) -> list[dict[str, Any]]:
    result = await session.execute(
        select(CamToken)
        .where(CamToken.org_id == identity.org_id, CamToken.user_id == target.id)
        .order_by(CamToken.created_at.desc(), CamToken.id.desc())
    )
    return [token_public(token) for token in result.scalars()]


async def list_audit_for_user(
    session: AsyncSession, identity: SessionIdentity, target: User, *, limit: int
) -> list[dict[str, Any]]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.org_id == identity.org_id, AuditLog.user_id == target.id)
        .order_by(AuditLog.ts.desc(), AuditLog.id.desc())
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars())
    return await _hydrate_audit_entries(session, rows)


async def list_org_audit_preview(
    session: AsyncSession, identity: SessionIdentity, *, limit: int
) -> list[dict[str, Any]]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.org_id == identity.org_id)
        .order_by(AuditLog.ts.desc(), AuditLog.id.desc())
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars())
    return await _hydrate_audit_entries(session, rows)


async def admin_overview_stats(session: AsyncSession, identity: SessionIdentity) -> dict[str, Any]:
    user_rows = (
        await session.execute(select(User.role, User.status).where(User.org_id == identity.org_id))
    ).all()
    users = {
        "total": len(user_rows),
        "active": sum(1 for _role, status in user_rows if status == "active"),
        "deactivated": sum(1 for _role, status in user_rows if status == "deactivated"),
        "admins": sum(1 for role, status in user_rows if role == "admin" and status == "active"),
    }
    service_rows = (
        await session.execute(
            select(Service.tier, Service.status, Service.health).where(
                Service.org_id == identity.org_id,
                Service.status != "deleted",
            )
        )
    ).all()
    by_tier: dict[str, int] = {"global": 0, "template": 0, "personal": 0}
    by_status: dict[str, int] = {}
    by_health: dict[str, int] = {}
    for tier, status, health in service_rows:
        by_tier[tier] = by_tier.get(tier, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        by_health[health] = by_health.get(health, 0) + 1
    return {
        "users": users,
        "services": {"by_tier": by_tier, "by_status": by_status, "by_health": by_health},
    }


def org_settings_public(org: Org) -> dict[str, Any]:
    settings = org.settings or {}
    return {
        "default_timeout_ms": int(settings.get("default_timeout_ms", 60000)),
        "response_cap_tokens": int(settings.get("response_cap_tokens", 50000)),
        "log_retention_days": int(settings.get("log_retention_days", 30)),
        "allowed_email_domains": list(settings.get("allowed_email_domains", [])),
        "instructions": str(settings.get("instructions", "")),
        "brand": dict(settings.get("brand") or {}),
    }


async def update_org_settings(
    session: AsyncSession, identity: SessionIdentity, updates: dict[str, Any]
) -> dict[str, Any]:
    org = await session.get(Org, identity.org_id)
    assert org is not None
    merged = dict(org.settings or {})
    merged.update(updates)
    org.settings = merged
    await session.flush()
    return org_settings_public(org)


def allowlist_public(row: UrlAllowlist, created_by_email: str | None) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "pattern": row.pattern,
        "note": row.note,
        "created_by_email": created_by_email,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def list_allowlist_entries(
    session: AsyncSession, identity: SessionIdentity
) -> list[dict[str, Any]]:
    rows = list(
        (
            await session.execute(
                select(UrlAllowlist)
                .where(UrlAllowlist.org_id == identity.org_id)
                .order_by(UrlAllowlist.pattern)
            )
        ).scalars()
    )
    creator_ids = {row.created_by for row in rows if row.created_by is not None}
    creators: dict[uuid.UUID, User] = {}
    if creator_ids:
        creator_rows = (
            await session.execute(select(User).where(User.id.in_(creator_ids)))
        ).scalars()
        creators = {user.id: user for user in creator_rows}
    entries: list[dict[str, Any]] = []
    for row in rows:
        creator_email = None
        if row.created_by is not None and row.created_by in creators:
            creator_email = creators[row.created_by].email
        entries.append(allowlist_public(row, creator_email))
    return entries


async def add_allowlist_entry(
    session: AsyncSession, identity: SessionIdentity, *, pattern: str, note: str | None
) -> dict[str, Any]:
    normalized = normalize_pattern(pattern)
    existing = await session.execute(
        select(UrlAllowlist.id).where(
            UrlAllowlist.org_id == identity.org_id, UrlAllowlist.pattern == normalized
        )
    )
    if existing.first() is not None:
        raise AllowlistError(f"pattern {normalized!r} is already allowlisted")
    row = UrlAllowlist(
        org_id=identity.org_id,
        pattern=normalized,
        note=note,
        created_by=identity.user.id,
    )
    session.add(row)
    await session.flush()
    return allowlist_public(row, identity.user.email)


async def delete_allowlist_entry(
    session: AsyncSession, identity: SessionIdentity, entry_id: uuid.UUID
) -> None:
    row = await session.get(UrlAllowlist, entry_id)
    if row is None or row.org_id != identity.org_id:
        raise ServiceNotFound(str(entry_id))
    await session.delete(row)
    await session.flush()
