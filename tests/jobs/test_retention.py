import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from cam.db.models import AuditLog
from cam.db.partitions import partition_bounds, partition_name
from cam.jobs.retention import (
    drop_partitions_older_than,
    ensure_partitions,
    list_partitions,
)


async def _org_id(db_session: AsyncSession) -> uuid.UUID:
    org = (await db_session.execute(text("SELECT id FROM orgs WHERE slug = 'indiamart'"))).first()
    assert org is not None
    return uuid.UUID(str(org[0]))


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _two_months_back(now: datetime) -> datetime:
    month = now.month - 2
    year = now.year
    while month < 1:
        month += 12
        year -= 1
    return datetime(year, month, 15, tzinfo=UTC)


def _partition_of(ts: datetime) -> str:
    return partition_name(ts.year, ts.month)


async def _ensure_partition(db_session: AsyncSession, ts: datetime) -> str:
    name = _partition_of(ts)
    start, end = partition_bounds(ts.year, ts.month)
    await db_session.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF audit_logs "
            f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
        )
    )
    await db_session.commit()
    return name


async def _audit_row(
    db_session: AsyncSession, *, org_id: uuid.UUID, ts: datetime, action: str
) -> uuid.UUID:
    row = AuditLog(org_id=org_id, action=action, status="ok", ts=ts)
    db_session.add(row)
    await db_session.commit()
    return row.id


async def test_ensure_partitions_idempotent(db_session: AsyncSession) -> None:
    first = await ensure_partitions(db_session)
    second = await ensure_partitions(db_session)
    assert second == []
    del first
    names = {name for name, _year, _month in await list_partitions(db_session)}
    assert _partition_of(_now()) in names


async def test_drop_partitions_older_than_drops_elapsed_months(
    db_session: AsyncSession,
) -> None:
    now = _now()
    old_ts = _two_months_back(now)
    old_partition = await _ensure_partition(db_session, old_ts)
    cutoff = now - timedelta(days=30)
    dropped = await drop_partitions_older_than(db_session, cutoff)
    assert old_partition in dropped
    assert _partition_of(now) not in set(dropped)


async def test_run_log_retention_removes_old_logs_and_keeps_recent(
    db_session: AsyncSession,
) -> None:
    from cam.db.session import create_session_factory
    from cam.jobs.retention import run_log_retention

    now = _now()
    org_id = await _org_id(db_session)
    recent_id = await _audit_row(db_session, org_id=org_id, ts=now, action="recent")

    old_ts = _two_months_back(now)
    old_partition = await _ensure_partition(db_session, old_ts)
    old_id = await _audit_row(db_session, org_id=org_id, ts=old_ts, action="old")

    engine = create_async_engine(os.environ["CAM_DATABASE_URL"])
    maker = create_session_factory(engine)
    dropped_count = await run_log_retention(maker)
    assert dropped_count >= 1

    remaining = {name for name, _year, _month in await list_partitions(db_session)}
    assert old_partition not in remaining
    assert _partition_of(now) in remaining

    gone = (
        await db_session.execute(select(AuditLog).where(AuditLog.id == old_id))
    ).scalar_one_or_none()
    assert gone is None
    kept = (
        await db_session.execute(select(AuditLog).where(AuditLog.id == recent_id))
    ).scalar_one_or_none()
    assert kept is not None

    await engine.dispose()
