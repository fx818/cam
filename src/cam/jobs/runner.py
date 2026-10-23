"""Background job scheduling: APScheduler inside the app lifecycle."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cam.api.metrics import set_scheduler_job_interval
from cam.config import Settings

logger = logging.getLogger(__name__)

JobFactory = Callable[[], Callable[[], object]]
SHUTDOWN_GRACE_S = 5.0


class JobRunner:
    """Runs health checks, re-sync and log retention on settings-driven intervals."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession] | None,
        *,
        make_health_job: Callable[[], Callable[[], object]] | None = None,
        make_resync_job: Callable[[], Callable[[], object]] | None = None,
        make_retention_job: Callable[[], Callable[[], object]] | None = None,
        shutdown_grace_s: float = SHUTDOWN_GRACE_S,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self._make_health_job = make_health_job
        self._make_resync_job = make_resync_job
        self._make_retention_job = make_retention_job
        self.scheduler: Any = None
        self._shutdown_grace_s = shutdown_grace_s
        # R27: tasks for jobs currently running, so aclose() can await/cancel
        # them before the DB engine and pool are torn down.
        self._running: set[asyncio.Task[Any]] = set()

    def start(self) -> None:
        if self.scheduler is not None:
            return
        if not self.settings.jobs_enabled:
            logger.info("background jobs disabled (CAM_JOBS_ENABLED off)")
            return
        scheduler = AsyncIOScheduler(timezone="UTC")
        now = datetime.now(tz=UTC)
        self._add(
            scheduler,
            self._make_health_job,
            "cam-health-check",
            interval=self.settings.healthcheck_interval_s,
            start_time=now,
        )
        self._add(
            scheduler,
            self._make_resync_job,
            "cam-resync",
            interval=self.settings.resync_interval_s,
            start_time=now,
        )
        self._add(
            scheduler,
            self._make_retention_job,
            "cam-log-retention",
            interval=timedelta(hours=24),
            start_time=now + timedelta(minutes=5),
        )
        scheduler.start()
        self.scheduler = scheduler
        logger.info(
            "background jobs scheduled (healthcheck=%ss, resync=%ss, retention=daily)",
            self.settings.healthcheck_interval_s,
            self.settings.resync_interval_s,
        )

    def track(self, job: Callable[[], Any]) -> Callable[[], Coroutine[Any, Any, None]]:
        """Wrap a job so :meth:`aclose` can await or cancel it (R27).

        Works for both sync and async job callables (APScheduler runs both).
        """

        async def wrapped() -> None:
            task = asyncio.current_task()
            if task is not None:
                self._running.add(task)
            try:
                result = job()
                if inspect.isawaitable(result):
                    await result
            finally:
                if task is not None:
                    self._running.discard(task)

        return wrapped

    async def aclose(self) -> None:
        scheduler: Any = self.scheduler
        self.scheduler = None
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)
        await self._drain_jobs()

    async def _drain_jobs(self) -> None:
        """Wait briefly for in-flight jobs, then cancel the stragglers."""
        tasks = [task for task in self._running if not task.done()]
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=self._shutdown_grace_s)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _add(
        self,
        scheduler: Any,
        factory: Callable[[], Callable[[], object]] | None,
        job_id: str,
        *,
        interval: int | timedelta,
        start_time: datetime,
    ) -> None:
        if factory is None:
            return
        job = self.track(factory())
        seconds = interval.total_seconds() if isinstance(interval, timedelta) else float(interval)
        # R28: expose each job's own cadence so alerts can be relative to it
        # (a 30-minute stall threshold is wrong for hourly/daily jobs).
        set_scheduler_job_interval(job_id, seconds)
        add_job: Any = scheduler.add_job
        add_job(
            job,
            trigger="interval",
            seconds=seconds,
            id=job_id,
            name=job_id,
            next_run_time=start_time,
            max_instances=1,
            coalesce=True,
        )
