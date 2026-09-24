"""Re-sync: pull the live upstream tool list and diff it into CAM.

Triggers: the scheduler, refresh from the dashboard and upstream
`notifications/tools/list_changed`. New tools follow the tier rule from the
decision log: personal services enable them immediately, global services and
templates hold them as `pending_review` (hidden until an admin approves them
in the review queue). Tools that disappear upstream are marked
`removed_upstream`, never deleted, so old audit entries still resolve.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cam.access.credentials import CredentialError, CredentialExpiredError, resolve_credential
from cam.access.resolution import service_ref
from cam.connections.url_policy import load_url_policy
from cam.db.models import Connection, Service, Tool, User
from cam.upstream import (
    ConnectionTarget,
    UpstreamError,
)
from cam.upstream.url import UrlPolicy

logger = logging.getLogger(__name__)

RESYNC_TIMEOUT_S = 30.0

ResyncStatus = Literal["done", "skipped", "failed"]


@dataclass(frozen=True, slots=True)
class UpstreamToolSpec:
    """One tool as the upstream advertises it (MCP `tools/list` or OpenAPI)."""

    name: str
    description: str
    input_schema: dict[str, Any]
    rest_binding: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ResyncOutcome:
    service_id: uuid.UUID
    ref: str
    status: ResyncStatus
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    error: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "added": self.added,
            "updated": self.updated,
            "removed": self.removed,
            "error": self.error,
        }


class ResyncError(Exception):
    pass


_locks: dict[uuid.UUID, asyncio.Lock] = {}


def lock_for(service_id: uuid.UUID) -> asyncio.Lock:
    return _locks.setdefault(service_id, asyncio.Lock())


async def _pick_connection(db: AsyncSession, service: Service) -> Connection | None:
    stmt = select(Connection).where(
        Connection.org_id == service.org_id, Connection.service_id == service.id
    )
    if service.tier == "personal" and service.owner_user_id is not None:
        stmt = stmt.where(Connection.user_id == service.owner_user_id)
    elif service.tier == "global":
        stmt = stmt.where(Connection.user_id.is_(None))
    stmt = stmt.order_by(Connection.created_at)
    rows = list((await db.execute(stmt)).scalars())
    connected = [row for row in rows if row.status == "connected"]
    if service.tier == "template":
        return connected[-1] if connected else None
    return connected[0] if connected else None


async def _target(db: AsyncSession, service: Service, connection: Connection) -> ConnectionTarget:
    url_policy = await load_url_policy(
        db, org_id=service.org_id, restricted=service.tier == "personal"
    )
    headers: dict[str, str] = {}
    if service.auth_type != "oauth" and connection.secret_enc is not None:
        try:
            material = await resolve_credential(db, service=service, connection=connection)
        except CredentialExpiredError as exc:
            raise ResyncError(
                f"The {service.name} connection has expired and must be reconnected."
            ) from exc
        except CredentialError as exc:
            raise ResyncError(f"The {service.name} credential is unusable: {exc}") from exc
        headers = dict(material.headers)
    url = service.url
    transport: Literal["streamable-http", "sse"] = (
        "streamable-http" if service.transport != "sse" else "sse"
    )
    return ConnectionTarget(
        service_id=str(service.id),
        connection_id=str(connection.id),
        url=url,
        transport=transport,
        headers=headers,
        url_policy=url_policy,
    )


async def _fetch_mcp_tools(
    db: AsyncSession, service: Service, connection: Connection
) -> list[UpstreamToolSpec]:
    from cam.upstream.client import UpstreamSession

    target = await _target(db, service, connection)
    session = UpstreamSession(target, call_timeout_s=RESYNC_TIMEOUT_S)
    try:
        await session.start()
        tools = await session.list_tools()
    except UpstreamError as exc:
        raise ResyncError(f"{service.name} is unreachable: {exc.code}") from exc
    except Exception as exc:
        raise ResyncError(f"{service.name} is unreachable") from exc
    finally:
        await session.aclose()
    return [
        UpstreamToolSpec(
            name=tool.name,
            description=tool.description,
            input_schema=dict(tool.input_schema),
        )
        for tool in tools
    ]


async def _fetch_rest_tools(db: AsyncSession, service: Service) -> list[UpstreamToolSpec]:
    from cam.registry.openapi import OpenAPIError, fetch_spec, parse_operations

    if not service.spec_url:
        raise ResyncError("this service has no spec_url, so there is nothing to re-sync")
    allowlist = await _allowlist(db, service)
    policy = UrlPolicy(restricted=service.tier == "personal", allowlist=allowlist)
    try:
        spec = await fetch_spec(service.spec_url, url_policy=policy)
    except (OpenAPIError, OSError) as exc:
        raise ResyncError(f"could not fetch the spec: {exc}") from exc
    try:
        operations = parse_operations(spec)
    except OpenAPIError as exc:
        raise ResyncError(str(exc)) from exc
    return [
        UpstreamToolSpec(
            name=operation.name,
            description=operation.description,
            input_schema=operation.input_schema,
            rest_binding=operation.binding,
        )
        for operation in operations
    ]


async def _allowlist(db: AsyncSession, service: Service) -> tuple[str, ...]:
    if service.tier != "personal":
        return ()
    from cam.connections.url_policy import load_url_allowlist

    return await load_url_allowlist(db, org_id=service.org_id)


def _new_tool_state(tier: str) -> tuple[bool, str]:
    """(enabled, status) for a tool the upstream lists but CAM has never seen."""
    if tier == "personal":
        return True, "active"
    return False, "pending_review"


def _reactivation_state(tier: str) -> tuple[str, bool]:
    """A tool marked removed_upstream that is listed upstream again follows
    the same tier rule as a genuinely new tool."""
    if tier == "personal":
        return "active", True
    return "pending_review", False


async def _ref(db: AsyncSession, service: Service) -> str:
    handle: str | None = None
    if service.owner_user_id is not None:
        owner = await db.get(User, service.owner_user_id)
        if owner is not None:
            handle = owner.handle
    try:
        return service_ref(service, handle)
    except ValueError:
        return f"unresolved/{service.slug}"


async def apply_diff(
    db: AsyncSession, service: Service, specs: list[UpstreamToolSpec]
) -> tuple[list[str], list[str], list[str]]:
    rows = list((await db.execute(select(Tool).where(Tool.service_id == service.id))).scalars())
    existing = {row.name: row for row in rows}
    spec_by_name = {spec.name: spec for spec in specs}

    added: list[str] = []
    updated: list[str] = []
    for name, spec in spec_by_name.items():
        row = existing.get(name)
        if row is None:
            enabled, status = _new_tool_state(service.tier)
            row = Tool(
                org_id=service.org_id,
                service_id=service.id,
                name=name,
                upstream_name=name,
                description=spec.description,
                input_schema=spec.input_schema,
                rest_binding=spec.rest_binding,
                enabled=enabled,
                status=status,
            )
            db.add(row)
            added.append(name)
            continue
        if row.status == "removed_upstream":
            status, enabled = _reactivation_state(service.tier)
            row.status = status
            row.enabled = enabled
            updated.append(name)
            if (
                spec.description
                and not row.description_override
                and row.description != spec.description
            ):
                row.description = spec.description
            if spec.input_schema and row.input_schema != spec.input_schema:
                row.input_schema = spec.input_schema
            if spec.rest_binding is not None and row.rest_binding != spec.rest_binding:
                row.rest_binding = spec.rest_binding
            continue
        if spec.description and row.description != spec.description:
            row.description = spec.description
            updated.append(name)
        if spec.input_schema and row.input_schema != spec.input_schema:
            row.input_schema = spec.input_schema
            if name not in updated:
                updated.append(name)
        if spec.rest_binding is not None and row.rest_binding != spec.rest_binding:
            row.rest_binding = spec.rest_binding
            if name not in updated:
                updated.append(name)

    removed: list[str] = []
    for name, row in existing.items():
        if name in spec_by_name:
            continue
        if row.status in ("active", "pending_review"):
            row.status = "removed_upstream"
            removed.append(name)

    awaiting = list(
        (
            await db.execute(
                select(Tool).where(
                    Tool.service_id == service.id,
                    Tool.status == "pending_review",
                )
            )
        ).scalars()
    )
    for row in awaiting:
        if not row.description_override and row.description and row.name in spec_by_name:
            spec = spec_by_name[row.name]
            if row.description != spec.description:
                row.description = spec.description
            row.input_schema = spec.input_schema
            row.rest_binding = spec.rest_binding

    service.last_synced_at = datetime.now(tz=UTC)
    await db.flush()
    return added, updated, removed


async def run_service_resync(
    db: AsyncSession,
    service: Service,
    *,
    connection: Connection | None = None,
) -> ResyncOutcome:
    """Diff against the live upstream list and persist the result."""
    ref = await _ref(db, service)
    chosen = connection if connection is not None else await _pick_connection(db, service)
    if chosen is None and service.kind == "mcp" and service.auth_type != "none":
        return ResyncOutcome(
            service_id=service.id,
            ref=ref,
            status="failed",
            error="no connected credential is stored for this service",
        )
    try:
        if service.kind == "rest":
            specs = await _fetch_rest_tools(db, service)
        elif chosen is not None:
            specs = await _fetch_mcp_tools(db, service, chosen)
        else:
            return ResyncOutcome(
                service_id=service.id,
                ref=ref,
                status="failed",
                error="no connected credential is stored for this service",
            )
    except ResyncError as exc:
        return ResyncOutcome(service_id=service.id, ref=ref, status="failed", error=str(exc))
    try:
        added, updated, removed = await apply_diff(db, service, specs)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.exception("re-sync apply failed for %s", ref)
        return ResyncOutcome(
            service_id=service.id, ref=ref, status="failed", error=f"re-sync failed: {exc}"
        )
    return ResyncOutcome(
        service_id=service.id,
        ref=ref,
        status="done",
        added=tuple(added),
        updated=tuple(updated),
        removed=tuple(removed),
    )


async def resync_service_id(
    session_factory: async_sessionmaker[AsyncSession],
    service_id: uuid.UUID,
    *,
    skip_if_running: bool = True,
) -> ResyncOutcome:
    """Re-sync one service by id with a per-service lock.

    Returns `skipped` when another re-sync is already running for this service.
    """
    lock = lock_for(service_id)
    if skip_if_running and lock.locked():
        async with session_factory() as db:
            service = await db.get(Service, service_id)
            ref = await _ref(db, service) if service else str(service_id)
        return ResyncOutcome(service_id=service_id, ref=ref, status="skipped")
    async with lock, session_factory() as db:
        service = await db.get(Service, service_id)
        if service is None or service.status == "deleted":
            return ResyncOutcome(
                service_id=service_id,
                ref=str(service_id),
                status="failed",
                error="service not found",
            )
        return await run_service_resync(db, service)


async def resync_by_ref(
    db: AsyncSession,
    service: Service,
    *,
    connection: Connection | None = None,
    skip_if_running: bool = False,
) -> ResyncOutcome:
    """Re-sync one loaded service (Refresh path). Takes the per-service lock."""
    lock = lock_for(service.id)
    if skip_if_running and lock.locked():
        return ResyncOutcome(service_id=service.id, ref=await _ref(db, service), status="skipped")
    async with lock:
        return await run_service_resync(db, service, connection=connection)


class ResyncCoordinator:
    """Reacting to `notifications/tools/list_changed`.

    The responsibility for the notification lands on the pool's message
    handlers, which call `enqueue`; a single worker task rakes pending
    services after a debounce window and re-syncs each one once.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        debounce_s: float = 2.0,
    ) -> None:
        self.session_factory = session_factory
        self.debounce_s = debounce_s
        self._pending: set[uuid.UUID] = set()
        self._event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="cam-resync-coordinator")

    async def aclose(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def enqueue(self, service_id: str | uuid.UUID) -> None:
        value = service_id if isinstance(service_id, uuid.UUID) else uuid.UUID(service_id)
        self._pending.add(value)
        self._event.set()

    def pending_count(self) -> int:
        return len(self._pending)

    async def _loop(self) -> None:
        while True:
            await self._event.wait()
            self._event.clear()
            await asyncio.sleep(self.debounce_s)
            items = list(self._pending)
            self._pending.clear()
            for service_id in items:
                try:
                    outcome = await resync_service_id(
                        self.session_factory, service_id, skip_if_running=True
                    )
                except Exception:
                    logger.exception("coordinated re-sync failed for %s", service_id)
                    continue
                logger.debug(
                    "resync %s -> status=%s added=%s updated=%s removed=%s",
                    outcome.ref,
                    outcome.status,
                    len(outcome.added),
                    len(outcome.updated),
                    len(outcome.removed),
                )
