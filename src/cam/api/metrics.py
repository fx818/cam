"""Prometheus metrics for CAM, exposed on GET /api/metrics.

Thin, registry-level instrumentation only: request counts by method, route
template and status, audit-writer queue instrumentation, upstream session
count, DB pool occupancy, service health counts and scheduler heartbeats.
Registry updates are cheap non-blocking counters, safe on the event loop.
"""

from __future__ import annotations

import time
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

REGISTRY = CollectorRegistry()

HTTP_REQUESTS = Counter(
    "cam_http_requests_total",
    "HTTP requests handled, by method, route template and status",
    ["method", "path", "status"],
    registry=REGISTRY,
)

HTTP_DURATION = Histogram(
    "cam_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
    registry=REGISTRY,
)

AUDIT_QUEUE_DEPTH = Gauge(
    "cam_audit_queue_depth",
    "Audit writer in-memory queue depth",
    registry=REGISTRY,
)

AUDIT_QUEUE_CAPACITY = Gauge(
    "cam_audit_queue_capacity",
    "Audit writer queue capacity (events dropped when full)",
    registry=REGISTRY,
)

AUDIT_DROPPED = Counter(
    "cam_audit_events_dropped_total",
    "Audit events dropped because the queue was full",
    registry=REGISTRY,
)

AUDIT_WRITTEN = Counter(
    "cam_audit_events_written_total",
    "Audit events written to Postgres, by action and status",
    ["action", "status"],
    registry=REGISTRY,
)

UPSTREAM_SESSIONS_ACTIVE = Gauge(
    "cam_upstream_sessions_active",
    "Live pooled upstream MCP sessions",
    registry=REGISTRY,
)

DB_POOL_SIZE = Gauge("cam_db_pool_size", "Configured SQLAlchemy pool size", registry=REGISTRY)

DB_POOL_IN_USE = Gauge(
    "cam_db_pool_checked_out_connections",
    "SQLAlchemy pool connections checked out",
    registry=REGISTRY,
)

DB_POOL_OVERFLOW = Gauge(
    "cam_db_pool_overflow_connections",
    "SQLAlchemy pool overflow connections in use",
    registry=REGISTRY,
)

SERVICES_BY_HEALTH = Gauge(
    "cam_services_by_health",
    "Active MCP services by last health status",
    ["health"],
    registry=REGISTRY,
)

SCHEDULER_JOB_LAST_RUN = Gauge(
    "cam_scheduler_job_last_run_timestamp_seconds",
    "Unix timestamp of the last scheduler job completion",
    ["job"],
    registry=REGISTRY,
)

SCHEDULER_JOB_ERRORS = Counter(
    "cam_scheduler_job_errors_total",
    "Scheduler job failures",
    ["job"],
    registry=REGISTRY,
)

HEALTH_LABELS = ("healthy", "failing", "needs_reconnect")


def set_service_health_counts(counts: dict[str, int]) -> None:
    for label in HEALTH_LABELS:
        SERVICES_BY_HEALTH.labels(label).set(max(0, counts.get(label, 0)))


def route_template(scope: dict[str, Any]) -> str:
    route = scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    return scope.get("path", "unknown")


class RequestMetricsMiddleware:
    """Pure-ASGI request counter. Path labels stay cardinality-safe because
    they use the matched route template (or the raw path when unmatched)."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") == "/api/metrics":
            await self._app(scope, receive, send)
            return
        method = scope["method"]
        recorded: list[tuple[str, float]] = []
        started = time.perf_counter()

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start" and not recorded:
                status = str(message["status"])
                path = route_template(scope)
                recorded.append((path, time.perf_counter() - started))
                HTTP_REQUESTS.labels(method, path, status).inc()
                HTTP_DURATION.labels(method, path).observe(recorded[0][1])
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        except Exception:
            HTTP_REQUESTS.labels(method, route_template(scope), "exception").inc()
            raise


def render() -> str:
    return generate_latest(REGISTRY).decode("utf-8")
