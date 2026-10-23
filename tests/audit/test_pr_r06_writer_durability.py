"""R06 regression tests: durable, accurately-counted audit writes.

Covers the two failure modes the finding calls out:

* a batch write failure must NOT be counted as written and must be retried a
  bounded number of times (terminal failures counted on a distinct metric);
* a full queue must make ``record()`` return ``False`` (the pipeline honours it).

These use an in-memory fake session factory so retry/failure paths are
exercised deterministically without a database.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from cam.api.metrics import REGISTRY
from cam.audit.writer import AuditEvent, AuditWriter


class _FakeSession:
    def __init__(self, factory: _FakeFactory) -> None:
        self._factory = factory
        self._pending: list[Any] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def add(self, row: Any) -> None:
        self._pending.append(row)

    async def commit(self) -> None:
        self._factory.commits += 1
        if self._factory.commits <= self._factory.failures:
            raise RuntimeError("simulated database failure")
        self._factory.rows.extend(self._pending)


class _FakeFactory:
    """Session factory whose first ``failures`` commits raise."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.sessions = 0
        self.commits = 0
        self.rows: list[Any] = []

    def __call__(self) -> _FakeSession:
        self.sessions += 1
        return _FakeSession(self)


def _event(*, action: str, marker: str) -> AuditEvent:
    return AuditEvent(org_id=uuid.uuid4(), action=action, args={"text": marker})


def _counter(name: str, labels: dict[str, str]) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else float(value)


async def test_failed_batch_is_retried_and_not_counted_as_written() -> None:
    action = f"pr06-retry-{uuid.uuid4().hex}"
    factory = _FakeFactory(failures=1)
    writer = AuditWriter(cast(Any, factory), max_write_attempts=3, retry_backoff_seconds=0.0)
    written_labels = {"action": action, "status": "ok"}
    written_before = _counter("cam_audit_events_written_total", written_labels)
    failed_before = _counter("cam_audit_events_write_failed_total", written_labels)

    try:
        await writer.start()
        assert writer.record(_event(action=action, marker="one")) is True
        await writer.flush()

        # The first commit failed, the retry succeeded: one extra session.
        assert factory.sessions == 2
        assert factory.commits == 2
        assert len(factory.rows) == 1
        # Written only counts after a commit, and nothing was abandoned.
        assert _counter("cam_audit_events_written_total", written_labels) - written_before == 1
        assert _counter("cam_audit_events_write_failed_total", written_labels) - failed_before == 0
    finally:
        await writer.aclose()


async def test_permanent_write_failure_is_bounded_and_visible() -> None:
    action = f"pr06-dead-{uuid.uuid4().hex}"
    factory = _FakeFactory(failures=999)
    writer = AuditWriter(cast(Any, factory), max_write_attempts=3, retry_backoff_seconds=0.0)
    written_labels = {"action": action, "status": "ok"}
    written_before = _counter("cam_audit_events_written_total", written_labels)
    failed_before = _counter("cam_audit_events_write_failed_total", written_labels)

    try:
        await writer.start()
        assert writer.record(_event(action=action, marker="lost")) is True
        await writer.flush()

        # Retried exactly max_write_attempts times, then abandoned.
        assert factory.sessions == 3
        assert factory.commits == 3
        assert factory.rows == []
        # Not counted as written; loss is visible on the distinct counter.
        assert _counter("cam_audit_events_written_total", written_labels) - written_before == 0
        assert _counter("cam_audit_events_write_failed_total", written_labels) - failed_before == 1
    finally:
        await writer.aclose()


async def test_retry_preserves_order_within_a_batch() -> None:
    action = f"pr06-order-{uuid.uuid4().hex}"
    factory = _FakeFactory(failures=1)
    writer = AuditWriter(cast(Any, factory), max_write_attempts=3, retry_backoff_seconds=0.0)
    try:
        await writer.start()
        for i in range(5):
            assert writer.record(_event(action=action, marker=str(i))) is True
        await writer.flush()

        assert [row.args["text"] for row in factory.rows] == ["0", "1", "2", "3", "4"]
    finally:
        await writer.aclose()


async def test_queue_full_returns_false() -> None:
    action = f"pr06-full-{uuid.uuid4().hex}"
    factory = _FakeFactory()
    writer = AuditWriter(cast(Any, factory), max_queue=1)
    dropped_before = _counter("cam_audit_events_dropped_total", {})
    try:
        assert writer.record(_event(action=action, marker="keep")) is True
        assert writer.record(_event(action=action, marker="dropped")) is False
        assert writer.queue_depth() == 1
        assert writer.queue_capacity() == 1
        await writer.flush()
        assert len(factory.rows) == 1
        assert _counter("cam_audit_events_dropped_total", {}) - dropped_before == 1
    finally:
        await writer.aclose()
