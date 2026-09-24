"""Service health: periodic probes plus invoke-driven updates.

Health values live on `services.health`: `unknown`, `healthy`, `failing`
(needs attention: the last result was an upstream failure) and
`needs_reconnect` (the stored credential expired and refresh failed). The
periodic probe is lightweight: one connect + `tools/list` per service with a
short timeout. REST services are health-monitored via invoke results only.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cam.access.credentials import CredentialError, CredentialExpiredError, resolve_credential
from cam.connections.url_policy import load_url_policy
from cam.db.models import Connection, Service
from cam.upstream import ConnectionTarget
from cam.upstream.errors import map_exception
from cam.upstream.url import UrlPolicy

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 15.0
CREDENTIAL_EXPIRED = "needs_reconnect"
INVOKE_IGNORED_CODES = frozenset({"CAM_RATE_LIMITED", "INTERNAL"})


def health_for_invoke(ok: bool, error_code: str | None) -> str | None:
    """Health value for an invoke outcome, or None when the result must not
    change the current value (rate limiting and internal errors are silent)."""
    if ok:
        return "healthy"
    if error_code is None:
        return None
    if error_code in INVOKE_IGNORED_CODES:
        return None
    if error_code == "CONNECTION_EXPIRED":
        return CREDENTIAL_EXPIRED
    return "failing"


@dataclass(frozen=True, slots=True)
class ProbeTarget:
    service_id: uuid.UUID
    connection_id: uuid.UUID
    url: str
    transport: str
    headers: dict[str, str]
    url_policy: UrlPolicy


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    service_id: uuid.UUID
    connection_id: uuid.UUID
    health: str


@dataclass(frozen=True, slots=True)
class ProbePlan:
    targets: list[ProbeTarget]
    expired: list[tuple[uuid.UUID, uuid.UUID]]


async def collect_targets(
    session_factory: async_sessionmaker[AsyncSession],
) -> ProbePlan:
    """Load MCP targets and decrypt credentials, then close the session
    before any upstream connection is opened."""
    targets: list[ProbeTarget] = []
    expired: list[tuple[uuid.UUID, uuid.UUID]] = []
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Service, Connection)
                .join(Connection, Connection.service_id == Service.id)
                .where(
                    Service.status == "active",
                    Service.kind == "mcp",
                    Service.transport.is_not(None),
                    Connection.status == "connected",
                )
            )
        ).all()
        for service, connection in rows:
            url_policy = await load_url_policy(
                db, org_id=service.org_id, restricted=service.tier == "personal"
            )
            try:
                material = await resolve_credential(db, service=service, connection=connection)
            except CredentialExpiredError:
                expired.append((service.id, connection.id))
                continue
            except CredentialError as exc:
                logger.info("health probe skipped: %s", exc)
                continue
            targets.append(
                ProbeTarget(
                    service_id=service.id,
                    connection_id=connection.id,
                    url=service.url,
                    transport=service.transport or "streamable-http",
                    headers=dict(material.headers),
                    url_policy=url_policy,
                )
            )
    return ProbePlan(targets=targets, expired=expired)


async def _probe(target: ProbeTarget) -> ProbeOutcome:
    from cam.upstream.client import UpstreamSession

    session = UpstreamSession(
        ConnectionTarget(
            service_id=str(target.service_id),
            connection_id=str(target.connection_id),
            url=target.url,
            transport="sse" if target.transport == "sse" else "streamable-http",
            headers=target.headers,
            url_policy=target.url_policy,
        ),
        call_timeout_s=PROBE_TIMEOUT_S,
    )
    try:
        await session.start()
        await session.list_tools()
    except Exception as exc:
        error, _dead = map_exception(exc, recent_statuses=())
        logger.debug(
            "health probe failed for service %s: %s",
            target.service_id,
            type(error).__name__,
        )
        return ProbeOutcome(target.service_id, target.connection_id, "failing")
    finally:
        await session.aclose()
    return ProbeOutcome(target.service_id, target.connection_id, "healthy")


async def run_health_checks(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Probe every active MCP service and persist one health badge per row."""
    plan = await collect_targets(session_factory)
    outcomes: list[ProbeOutcome] = []
    results = await asyncio.gather(*[_probe(target) for target in plan.targets])
    outcomes.extend(results)
    for service_id, connection_id in plan.expired:
        outcomes.append(ProbeOutcome(service_id, connection_id, CREDENTIAL_EXPIRED))
    await _announce(session_factory, outcomes)
    return len(outcomes)


async def _announce(
    session_factory: async_sessionmaker[AsyncSession], outcomes: list[ProbeOutcome]
) -> None:
    if not outcomes:
        return
    async with session_factory() as db:
        for outcome in outcomes:
            await db.execute(
                update(Service)
                .where(Service.id == outcome.service_id)
                .values(health=outcome.health)
            )
            await db.execute(
                update(Connection)
                .where(Connection.id == outcome.connection_id)
                .values(
                    status=(
                        Connection.status
                        if outcome.health != CREDENTIAL_EXPIRED
                        else "needs_reconnect"
                    )
                )
            )
        await db.commit()


async def record_invoke_health(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    service_id: uuid.UUID,
    connection_id: uuid.UUID,
    ok: bool,
    error_code: str | None,
) -> None:
    health = health_for_invoke(ok, error_code)
    if health is None:
        return
    async with session_factory() as db:
        await db.execute(update(Service).where(Service.id == service_id).values(health=health))
        if health == CREDENTIAL_EXPIRED:
            await db.execute(
                update(Connection)
                .where(Connection.id == connection_id, Connection.status == "connected")
                .values(status="needs_reconnect")
            )
        await db.commit()
