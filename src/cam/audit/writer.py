"""Async audit-log writer: a bounded in-process buffer drained by one task.

Events are captured before credentials are involved (tool arguments never
contain secrets), queued without blocking the request, and written in batches
by a single background task.

Durability guarantee
--------------------
``record()`` enqueues an event without blocking the request path and returns
``True`` when the event was accepted (``False`` when the writer is closed or
the bounded queue is full). Accepted events live only in memory.

A batch is counted in ``AUDIT_WRITTEN`` only after its Postgres transaction
commits, so the metric reflects rows that actually reached the database. A
failed write is retried in place a bounded number of times with backoff before
the batch is abandoned; abandoned events are counted in ``AUDIT_WRITE_FAILED``
and events rejected because the queue was full in ``AUDIT_DROPPED``, so loss is
visible instead of being silently reported as success. Retrying an audit write
is *not* a retry of the upstream tool call.

Durability is best-effort against a process crash: because the buffer is
deliberately in-memory and bounded to keep the request path non-blocking,
events that are queued or mid-retry are lost if the process dies before the
background task drains them. Callers that need certainty should check
``record()``'s return value.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from prometheus_client import Counter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cam.api.metrics import AUDIT_DROPPED, AUDIT_WRITTEN, REGISTRY
from cam.db.models import AuditLog

logger = logging.getLogger(__name__)

# Distinct from AUDIT_DROPPED (queue-full rejections): events that were accepted
# but could not be persisted after all write retries were exhausted. Registered
# on the shared metrics registry so it surfaces on GET /api/metrics.
AUDIT_WRITE_FAILED = Counter(
    "cam_audit_events_write_failed_total",
    "Audit events abandoned after all write retries were exhausted",
    ["action", "status"],
    registry=REGISTRY,
)

# Cap a single retry backoff so a wildly-backed-off attempt can't stall the
# drain loop for an unbounded time.
_RETRY_BACKOFF_CAP_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class AuditRef:
    service_id: uuid.UUID | None
    connection_id: uuid.UUID | None = None
    tool_name: str | None = None
    owner_user_id: uuid.UUID | None = None
    skill_version_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    org_id: uuid.UUID
    action: str
    user_id: uuid.UUID | None = None
    token_id: uuid.UUID | None = None
    agent_name: str | None = None
    agent_version: str | None = None
    service_id: uuid.UUID | None = None
    connection_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None
    tool_name: str | None = None
    skill_version_id: uuid.UUID | None = None
    args: dict[str, Any] | None = None
    status: str = "ok"
    error_code: str | None = None
    error_message: str | None = None
    latency_ms: int | None = None
    response_bytes: int | None = None
    truncated: bool = False

    def from_ref(self, ref: AuditRef | None) -> AuditEvent:
        if ref is None:
            return self
        return AuditEvent(
            org_id=self.org_id,
            action=self.action,
            user_id=self.user_id,
            token_id=self.token_id,
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            service_id=ref.service_id,
            connection_id=ref.connection_id,
            owner_user_id=ref.owner_user_id,
            tool_name=ref.tool_name,
            skill_version_id=ref.skill_version_id
            if ref.skill_version_id is not None
            else self.skill_version_id,
            args=self.args,
            status=self.status,
            error_code=self.error_code,
            error_message=self.error_message,
            latency_ms=self.latency_ms,
            response_bytes=self.response_bytes,
            truncated=self.truncated,
        )

    def to_row(self) -> AuditLog:
        return AuditLog(
            org_id=self.org_id,
            user_id=self.user_id,
            token_id=self.token_id,
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            action=self.action,
            service_id=self.service_id,
            connection_id=self.connection_id,
            owner_user_id=self.owner_user_id,
            tool_name=self.tool_name,
            skill_version_id=self.skill_version_id,
            args=self.args,
            status=self.status,
            error_code=self.error_code,
            error_message=self.error_message,
            latency_ms=self.latency_ms,
            response_bytes=self.response_bytes,
            truncated=self.truncated,
        )


class AuditWriter:
    """Bounded, non-blocking audit buffer drained by one background task.

    The queue bounds memory to ``max_queue`` events. The single drain task
    claims at most ``batch_size`` events at a time and retries a failed batch
    *in place* (never re-queueing it behind newer events), so ordering is
    preserved and the retry buffer is bounded by the batch size.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_queue: int = 1000,
        batch_size: int = 50,
        max_write_attempts: int = 3,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        if max_queue < 1:
            raise ValueError("max_queue must be >= 1")
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if max_write_attempts < 1:
            raise ValueError("max_write_attempts must be >= 1")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be >= 0")
        self._session_factory = session_factory
        self._queue: asyncio.Queue[AuditEvent] = asyncio.Queue(maxsize=max_queue)
        self._batch_size = batch_size
        self._max_write_attempts = max_write_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._run(), name="cam-audit-writer")

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def queue_capacity(self) -> int:
        return self._queue.maxsize

    def record(self, event: AuditEvent) -> bool:
        """Enqueue an event without blocking.

        Returns ``True`` when the event was accepted into the bounded queue and
        ``False`` when it was not (writer closed, or queue full). The invoke
        pipeline honours this return value.
        """
        if self._closed:
            return False
        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            logger.warning("audit queue full; dropping %s event", event.action)
            AUDIT_DROPPED.inc()
            return False

    async def flush(self) -> None:
        if self._task is None and not self._closed and not self._queue.empty():
            await self.start()
        await self._queue.join()

    async def aclose(self) -> None:
        if self._closed:
            return
        if self._task is None and not self._queue.empty():
            await self.start()
        self._closed = True
        await self._queue.join()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            batch = [event]
            while len(batch) < self._batch_size:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                if await self._write_with_retries(batch):
                    for item in batch:
                        AUDIT_WRITTEN.labels(item.action, item.status).inc()
                else:
                    for item in batch:
                        AUDIT_WRITE_FAILED.labels(item.action, item.status).inc()
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _write_with_retries(self, batch: list[AuditEvent]) -> bool:
        """Persist ``batch`` in place, retrying a bounded number of times.

        Returns ``True`` once a commit succeeds. On exhaustion the batch is
        abandoned (the events are lost) and ``False`` is returned so the caller
        can count them without marking them as written. Because the retry
        happens before the next batch is claimed, event ordering is preserved
        and no separate (unbounded) retry buffer is needed.
        """
        for attempt in range(1, self._max_write_attempts + 1):
            if await self._write(batch):
                return True
            if attempt < self._max_write_attempts:
                backoff = min(
                    self._retry_backoff_seconds * (2 ** (attempt - 1)),
                    _RETRY_BACKOFF_CAP_SECONDS,
                )
                if backoff > 0:
                    await asyncio.sleep(backoff)
        logger.error(
            "audit batch permanently failed after %d attempts; %d event(s) lost",
            self._max_write_attempts,
            len(batch),
        )
        return False

    async def _write(self, batch: list[AuditEvent]) -> bool:
        """Write one batch in a single transaction. Returns whether it committed."""
        try:
            async with self._session_factory() as session:
                for event in batch:
                    session.add(event.to_row())
                await session.commit()
        except Exception:
            logger.warning("audit batch write failed (%d events)", len(batch), exc_info=True)
            return False
        return True
