"""R27: bounded, deterministic health probing and job shutdown."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from cam.config import Settings
from cam.jobs import health as health_module
from cam.jobs.health import ProbeOutcome, ProbeTarget, aggregate_service_health
from cam.jobs.runner import JobRunner
from cam.upstream.url import UrlPolicy


def _outcome(service_id: uuid.UUID, health: str) -> ProbeOutcome:
    return ProbeOutcome(service_id, uuid.uuid4(), health)


def test_aggregate_service_health_is_worst_case_and_order_independent() -> None:
    a = uuid.uuid4()
    b = uuid.uuid4()
    outcomes = [
        _outcome(a, "healthy"),
        _outcome(a, "failing"),
        _outcome(b, "failing"),
        _outcome(b, "needs_reconnect"),
    ]
    aggregate = aggregate_service_health(outcomes)
    assert aggregate[a] == "failing"
    assert aggregate[b] == "needs_reconnect"
    assert aggregate_service_health(list(reversed(outcomes))) == aggregate
    assert len(aggregate) == 2


def _targets(count: int) -> list[ProbeTarget]:
    return [
        ProbeTarget(
            service_id=uuid.uuid4(),
            connection_id=uuid.uuid4(),
            url="http://example.test/mcp",
            transport="streamable-http",
            headers={},
            url_policy=UrlPolicy(),
        )
        for _ in range(count)
    ]


async def test_probe_concurrency_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    peak = 0

    async def fake_probe(target: ProbeTarget) -> ProbeOutcome:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return ProbeOutcome(target.service_id, target.connection_id, "healthy")

    monkeypatch.setattr(health_module, "_probe", fake_probe)
    probe_targets = health_module._probe_targets  # pyright: ignore[reportPrivateUsage]
    outcomes = await probe_targets(_targets(40))
    assert len(outcomes) == 40
    assert peak <= health_module.HEALTH_PROBE_CONCURRENCY
    assert peak > 1


async def test_aclose_cancels_tracked_jobs() -> None:
    runner = JobRunner(Settings(jobs_enabled=False), None, shutdown_grace_s=0.0)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow() -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(runner.track(slow)())
    await started.wait()
    await runner.aclose()
    assert task.done()
    assert cancelled.is_set()


async def test_aclose_awaits_jobs_that_finish() -> None:
    runner = JobRunner(Settings(jobs_enabled=False), None, shutdown_grace_s=5.0)
    done = asyncio.Event()

    async def quick() -> None:
        await asyncio.sleep(0.01)
        done.set()

    task = asyncio.create_task(runner.track(quick)())
    await asyncio.sleep(0)
    await runner.aclose()
    assert done.is_set()
    assert task.done()
