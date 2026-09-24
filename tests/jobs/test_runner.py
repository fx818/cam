import asyncio

from cam.config import Settings
from cam.jobs.runner import JobRunner


def make_counter(into: list[int]):
    def job_factory():
        def run() -> None:
            into.append(1)

        return run

    return job_factory


async def test_jobs_disabled_leaves_scheduler_unset() -> None:
    cfg = Settings(jobs_enabled=False)
    runner = JobRunner(cfg, None)
    runner.start()
    assert runner.scheduler is None
    await runner.aclose()


async def test_interval_job_runs_on_schedule() -> None:
    counter: list[int] = []
    cfg = Settings(jobs_enabled=True, healthcheck_interval_s=1)
    runner = JobRunner(
        cfg,
        None,
        make_health_job=make_counter(counter),
    )
    runner.start()
    assert runner.scheduler is not None
    try:
        await asyncio.sleep(1.5)
        assert len(counter) >= 1
        assert len(counter) <= 4
    finally:
        await runner.aclose()
    assert runner.scheduler is None


async def test_close_is_idempotent() -> None:
    runner = JobRunner(Settings(jobs_enabled=False), None)
    await runner.aclose()
    await runner.aclose()
