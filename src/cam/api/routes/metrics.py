"""GET /api/metrics: Prometheus exposition for internal scraping.

Unauthenticated on purpose (counters only; like /api/health). Restrict it to
the monitoring network at the ingress or with a NetworkPolicy in production.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncEngine

from cam.api.metrics import (
    AUDIT_QUEUE_CAPACITY,
    AUDIT_QUEUE_DEPTH,
    CONTENT_TYPE,
    DB_POOL_IN_USE,
    DB_POOL_OVERFLOW,
    DB_POOL_SIZE,
    UPSTREAM_SESSIONS_ACTIVE,
    render,
)
from cam.audit.writer import AuditWriter
from cam.upstream.pool import SessionPool

router = APIRouter(tags=["metrics"])


def refresh_in_process_gauges(app_state: object) -> None:
    AUDIT_QUEUE_CAPACITY.set(0)
    AUDIT_QUEUE_DEPTH.set(0)
    UPSTREAM_SESSIONS_ACTIVE.set(0)
    DB_POOL_SIZE.set(0)
    DB_POOL_IN_USE.set(0)
    DB_POOL_OVERFLOW.set(0)
    audit: AuditWriter | None = getattr(app_state, "audit", None)
    if audit is not None:
        AUDIT_QUEUE_CAPACITY.set(audit.queue_capacity())
        AUDIT_QUEUE_DEPTH.set(audit.queue_depth())
    pool: SessionPool | None = getattr(app_state, "pool", None)
    if pool is not None:
        UPSTREAM_SESSIONS_ACTIVE.set(pool.active_count())
    engine: AsyncEngine | None = getattr(app_state, "engine", None)
    if engine is not None:
        sync_pool: Any = engine.pool
        DB_POOL_SIZE.set(int(sync_pool.size()))
        DB_POOL_IN_USE.set(int(sync_pool.checkedout()))
        DB_POOL_OVERFLOW.set(max(0, int(sync_pool.overflow())))


@router.get("/api/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    refresh_in_process_gauges(request.app.state)
    return Response(content=render(), media_type=CONTENT_TYPE)
