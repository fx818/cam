"""Background job scheduling: APScheduler inside the app lifecycle."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cam.config import Settings

logger = logging.getLogger(__name__)

JobFactory = Callable[[], Callable[[], object]]


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
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self._make_health_job = make_health_job
        self._make_resync_job = make_resync_job
        self._make_retention_job = make_retention_job
        self.scheduler: Any = None

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

    async def aclose(self) -> None:
        scheduler: Any = self.scheduler
        self.scheduler = None
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)

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
        job = factory()
        add_job: Any = scheduler.add_job
        add_job(
            job,
            trigger="interval",
            seconds=(interval.total_seconds() if isinstance(interval, timedelta) else interval),
            id=job_id,
            name=job_id,
            next_run_time=start_time,
            max_instances=1,
            coalesce=True,
        )
