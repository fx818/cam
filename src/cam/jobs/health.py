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
from cam.api.metrics import set_service_health_counts
from cam.connections.url_policy import load_url_policy
from cam.db.models import Connection, Service
from cam.upstream import ConnectionTarget
from cam.upstream.errors import map_exception
from cam.upstream.url import UrlPolicy, url_with_credential_query

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 15.0
CREDENTIAL_EXPIRED = "needs_reconnect"
INVOKE_IGNORED_CODES = frozenset({"CAM_RATE_LIMITED", "INTERNAL"})

# R27: bound how many upstream probes run at once instead of an unbounded gather.
HEALTH_PROBE_CONCURRENCY = 8

# Worst-outcome precedence for aggregating several connections to one service.
_HEALTH_RANK = {"healthy": 0, "failing": 1, "needs_reconnect": 2}


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
                    url=url_with_credential_query(service.url, dict(material.query)),
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


def aggregate_service_health(outcomes: list[ProbeOutcome]) -> dict[uuid.UUID, str]:
    """Collapse per-connection outcomes to one deterministic health per service.

    R27: a service with several connections (for example a template connected by
    more than one user) takes the worst outcome, with ``needs_reconnect``
    outranking ``failing`` and ``failing`` outranking ``healthy`` — deterministic
    regardless of probe completion order.
    """
    per_service: dict[uuid.UUID, str] = {}
    for outcome in outcomes:
        current = per_service.get(outcome.service_id)
        if current is None or _HEALTH_RANK.get(outcome.health, 0) > _HEALTH_RANK.get(current, 0):
            per_service[outcome.service_id] = outcome.health
    return per_service


async def _probe_targets(targets: list[ProbeTarget]) -> list[ProbeOutcome]:
    """Probe targets with bounded concurrency (R27: no unbounded gather)."""
    if not targets:
        return []
    semaphore = asyncio.Semaphore(HEALTH_PROBE_CONCURRENCY)

    async def guarded(target: ProbeTarget) -> ProbeOutcome:
        async with semaphore:
            return await _probe(target)

    return list(await asyncio.gather(*(guarded(target) for target in targets)))


async def run_health_checks(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Probe every active MCP service and persist one health badge per service.

    Per-connection outcomes are recorded and aggregated deterministically; the
    returned count and the health gauge count distinct services (R27, R28).
    """
    plan = await collect_targets(session_factory)
    outcomes: list[ProbeOutcome] = list(await _probe_targets(plan.targets))
    for service_id, connection_id in plan.expired:
        outcomes.append(ProbeOutcome(service_id, connection_id, CREDENTIAL_EXPIRED))
    per_service = aggregate_service_health(outcomes)
    counts: dict[str, int] = {}
    for health in per_service.values():
        counts[health] = counts.get(health, 0) + 1
    set_service_health_counts(counts)
    await _announce(session_factory, per_service, outcomes)
    return len(per_service)


async def _announce(
    session_factory: async_sessionmaker[AsyncSession],
    per_service: dict[uuid.UUID, str],
    outcomes: list[ProbeOutcome],
) -> None:
    if not per_service:
        return
    async with session_factory() as db:
        # One write per service (the deterministic aggregate), so concurrent
        # per-connection outcomes cannot race to overwrite each other.
        for service_id, health in per_service.items():
            await db.execute(update(Service).where(Service.id == service_id).values(health=health))
        for outcome in outcomes:
            if outcome.health == CREDENTIAL_EXPIRED:
                await db.execute(
                    update(Connection)
                    .where(
                        Connection.id == outcome.connection_id,
                        Connection.status == "connected",
                    )
                    .values(status="needs_reconnect")
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
