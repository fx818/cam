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

INVOKE_ACTIONS = Counter(
    "cam_invoke_actions_total",
    "cam tool actions by action and outcome (ok/error); MCP tool errors "
    "surface here as HTTP 200 responses, so alert on this, not on HTTP status",
    ["action", "outcome"],
    registry=REGISTRY,
)

INVOKE_ERRORS = Counter(
    "cam_invoke_errors_total",
    "Failed cam tool actions by action and bounded error code",
    ["action", "code"],
    registry=REGISTRY,
)

SCHEDULER_JOB_INTERVAL = Gauge(
    "cam_scheduler_job_interval_seconds",
    "Configured interval between scheduled runs of a background job "
    "(lets alerts fire relative to each job's own cadence)",
    ["job"],
    registry=REGISTRY,
)

HEALTH_LABELS = ("healthy", "failing", "needs_reconnect")

# Bounded label domains for the invoke metrics. Anything outside these sets is
# folded into a fixed catch-all so a caller cannot blow up label cardinality.
CAM_INVOKE_ACTIONS = frozenset(
    {
        "list_services",
        "list_tools",
        "describe",
        "invoke",
        "list_skills",
        "load_skill",
        "read_skill_file",
    }
)
CAM_INVOKE_ERROR_CODES = frozenset(
    {
        "NOT_FOUND",
        "INVALID_ARGS",
        "CONNECTION_EXPIRED",
        "OWNER_DEACTIVATED",
        "SERVICE_DISABLED",
        "SKILL_UNAVAILABLE",
        "UPSTREAM_AUTH",
        "UPSTREAM_ERROR",
        "UPSTREAM_RATE_LIMITED",
        "CAM_RATE_LIMITED",
        "TIMEOUT",
        "UPSTREAM_UNAVAILABLE",
        "INTERNAL",
    }
)
OTHER_LABEL = "other"
UNKNOWN_LABEL = "UNKNOWN"
UNMATCHED_ROUTE = "<unmatched>"


def set_service_health_counts(counts: dict[str, int]) -> None:
    """Set the health gauge. ``counts`` must count *distinct services* per
    health value, not probe outcomes, so a service with several connections is
    counted once."""
    for label in HEALTH_LABELS:
        SERVICES_BY_HEALTH.labels(label).set(max(0, counts.get(label, 0)))


def record_invoke_outcome(action: str, *, is_error: bool, error_code: str | None = None) -> None:
    """Record one cam tool action at the pipeline boundary.

    Called by the invoke pipeline for every action so tool-level failures are
    observable even though the MCP call itself returns HTTP 200. Labels are
    bounded: an unknown action collapses to ``other`` and an unknown error code
    to ``UNKNOWN``.
    """
    action_label = action if action in CAM_INVOKE_ACTIONS else OTHER_LABEL
    outcome = "error" if is_error else "ok"
    INVOKE_ACTIONS.labels(action_label, outcome).inc()
    if is_error:
        code_label = error_code if error_code in CAM_INVOKE_ERROR_CODES else UNKNOWN_LABEL
        INVOKE_ERRORS.labels(action_label, code_label).inc()


def set_scheduler_job_interval(job: str, seconds: float) -> None:
    SCHEDULER_JOB_INTERVAL.labels(job).set(seconds)


def route_template(scope: dict[str, Any]) -> str:
    route = scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    # Unmatched requests (404s, unknown paths) must not become raw metric
    # labels: that is an unbounded-cardinality label driven by the caller.
    return UNMATCHED_ROUTE


class RequestMetricsMiddleware:
    """Pure-ASGI request counter. Path labels stay cardinality-safe because
    they use the matched route template, or a single fixed `<unmatched>`
    label when no route matched (never the raw request path)."""

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
