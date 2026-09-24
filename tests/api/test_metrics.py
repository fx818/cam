"""Phase 8.4: the Prometheus /api/metrics endpoint and registry."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import httpx
import pytest

from api.helpers import api_client
from cam.api.main import create_app
from cam.api.metrics import HEALTH_LABELS, REGISTRY, set_service_health_counts
from cam.audit.writer import AuditEvent, AuditWriter

METRIC_NAMES = (
    "cam_http_requests_total",
    "cam_http_request_duration_seconds",
    "cam_audit_queue_depth",
    "cam_audit_queue_capacity",
    "cam_audit_events_written_total",
    "cam_audit_events_dropped_total",
    "cam_upstream_sessions_active",
    "cam_db_pool_size",
    "cam_db_pool_checked_out_connections",
    "cam_db_pool_overflow_connections",
    "cam_services_by_health",
    "cam_scheduler_job_last_run_timestamp_seconds",
    "cam_scheduler_job_errors_total",
)


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient]:
    async with api_client(create_app()) as httpx_client:
        yield httpx_client


async def scrape(client: httpx.AsyncClient) -> str:
    response = await client.get("/api/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    return response.text


async def test_metrics_exposes_registry(client: httpx.AsyncClient) -> None:
    body = await scrape(client)
    for name in METRIC_NAMES:
        assert name in body


async def test_metrics_excludes_itself(client: httpx.AsyncClient) -> None:
    body = await scrape(client)
    assert 'path="/api/metrics"' not in body


async def test_metrics_counts_requests_by_status(client: httpx.AsyncClient) -> None:
    await scrape(client)
    await client.get("/api/health")
    body = await scrape(client)
    assert 'path="/api/health"' in body
    assert 'method="GET"' in body
    assert 'status="200"' in body


async def test_metrics_in_process_gauges(client: httpx.AsyncClient) -> None:
    await scrape(client)
    assert REGISTRY.get_sample_value("cam_audit_queue_capacity", {}) or 0 > 0
    depth = REGISTRY.get_sample_value("cam_audit_queue_depth", {})
    sessions = REGISTRY.get_sample_value("cam_upstream_sessions_active", {})
    pool = REGISTRY.get_sample_value("cam_db_pool_size", {})
    in_use = REGISTRY.get_sample_value("cam_db_pool_checked_out_connections", {})
    assert depth == 0
    assert sessions == 0
    assert (pool or 0) > 0
    assert in_use == 0


async def test_service_health_gauge() -> None:
    set_service_health_counts({"healthy": 2, "failing": 1})
    assert REGISTRY.get_sample_value("cam_services_by_health", {"health": "healthy"}) == 2
    assert REGISTRY.get_sample_value("cam_services_by_health", {"health": "failing"}) == 1
    assert REGISTRY.get_sample_value("cam_services_by_health", {"health": "needs_reconnect"}) == 0
    assert len(HEALTH_LABELS) == 3


async def test_audit_writer_instrumentation() -> None:
    writer = AuditWriter(cast(Any, None), max_queue=1, batch_size=1)
    try:
        event = AuditEvent(org_id=uuid.uuid4(), action="invoke")
        assert writer.record(event) is True
        assert writer.record(event) is False
        assert writer.queue_depth() == 1
        assert writer.queue_capacity() == 1
    finally:
        await writer.aclose()

    dropped = REGISTRY.get_sample_value("cam_audit_events_dropped_total", {})
    assert dropped is not None
    assert dropped >= 1
    assert writer.record(event) is False
