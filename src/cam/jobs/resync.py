"""Periodic re-sync job: run the upstream diff for every live service."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cam.db.models import Service
from cam.registry.resync import resync_service_id

logger = logging.getLogger(__name__)


async def run_resync_all(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as db:
        service_ids = list(
            (
                await db.execute(
                    select(Service.id).where(
                        Service.status == "active",
                        Service.kind.in_(("mcp", "rest")),
                    )
                )
            ).scalars()
        )
    done = 0
    for service_id in service_ids:
        try:
            outcome = await resync_service_id(session_factory, service_id)
        except Exception:
            logger.exception("scheduled re-sync failed for %s", service_id)
            continue
        if outcome.status == "done":
            done += 1
        logger.debug(
            "scheduled resync %s -> %s",
            outcome.ref,
            outcome.status,
        )
    return done
