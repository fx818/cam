"""Async audit-log writer: a bounded in-process buffer drained by one task.

Events are captured before credentials are involved (tool arguments never
contain secrets), queued without blocking the request, and written in batches.
A full queue drops the event with a warning rather than failing the call.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cam.db.models import AuditLog

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuditRef:
    service_id: uuid.UUID
    connection_id: uuid.UUID | None = None
    tool_name: str | None = None
    owner_user_id: uuid.UUID | None = None


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
            skill_version_id=self.skill_version_id,
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
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_queue: int = 1000,
        batch_size: int = 50,
    ) -> None:
        if max_queue < 1:
            raise ValueError("max_queue must be >= 1")
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self._session_factory = session_factory
        self._queue: asyncio.Queue[AuditEvent] = asyncio.Queue(maxsize=max_queue)
        self._batch_size = batch_size
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._run(), name="cam-audit-writer")

    def record(self, event: AuditEvent) -> bool:
        if self._closed:
            return False
        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            logger.warning("audit queue full; dropping %s event", event.action)
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
                await self._write(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _write(self, batch: list[AuditEvent]) -> None:
        try:
            async with self._session_factory() as session:
                for event in batch:
                    session.add(event.to_row())
                await session.commit()
        except Exception:
            logger.warning("audit batch write failed (%d events)", len(batch), exc_info=True)
