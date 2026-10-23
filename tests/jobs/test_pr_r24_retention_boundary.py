"""R24: retention drops expired rows in the boundary month, not just whole
elapsed partitions."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from cam.config import Settings
from cam.connections.orgs import ensure_org
from cam.db.models import AuditLog
from cam.db.partitions import partition_bounds, partition_name
from cam.db.session import create_session_factory
from cam.jobs import retention as retention_module
from cam.jobs.retention import delete_expired_rows, list_partitions, run_log_retention


async def _org_id(db_session: AsyncSession) -> uuid.UUID:
    org = (await db_session.execute(text("SELECT id FROM orgs WHERE slug = 'indiamart'"))).first()
    assert org is not None
    return uuid.UUID(str(org[0]))


async def _ensure_partition(db_session: AsyncSession, ts: datetime) -> str:
    name = partition_name(ts.year, ts.month)
    start, end = partition_bounds(ts.year, ts.month)
    await db_session.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF audit_logs "
            f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
        )
    )
    await db_session.commit()
    return name


async def _row(
    db_session: AsyncSession, *, org_id: uuid.UUID, ts: datetime, action: str
) -> uuid.UUID:
    row = AuditLog(org_id=org_id, action=action, status="ok", ts=ts)
    db_session.add(row)
    await db_session.commit()
    return row.id


async def _fetch(db_session: AsyncSession, row_id: uuid.UUID) -> AuditLog | None:
    return (
        await db_session.execute(select(AuditLog).where(AuditLog.id == row_id))
    ).scalar_one_or_none()


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def test_delete_expired_rows_removes_only_pre_cutoff_rows(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(tz=UTC)
    org_id = await _org_id(db_session)
    month = _month_start(now) - timedelta(days=45)
    await _ensure_partition(db_session, month)
    cutoff = month.replace(day=15)
    old_id = await _row(db_session, org_id=org_id, ts=cutoff - timedelta(days=3), action="old")
    new_id = await _row(db_session, org_id=org_id, ts=cutoff + timedelta(days=3), action="new")

    deleted = await delete_expired_rows(db_session, cutoff)

    assert deleted >= 1
    assert await _fetch(db_session, old_id) is None
    assert await _fetch(db_session, new_id) is not None


async def test_run_log_retention_clears_boundary_month_rows(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolate the R24 behaviour from org-configured retention so the cutoff is
    # exactly what the test controls.
    monkeypatch.setattr(retention_module, "_effective_settings", None)
    now = datetime.now(tz=UTC)
    org_id = await _org_id(db_session)
    this_month = _month_start(now)
    # cutoff lands inside last month; rows in that same (boundary) month that are
    # older than the cutoff must be deleted even though the partition survives.
    cutoff = this_month - timedelta(days=15)
    expired_ts = this_month - timedelta(days=25)
    kept_ts = this_month - timedelta(days=5)
    old_month_ts = this_month - timedelta(days=45)
    await _ensure_partition(db_session, expired_ts)
    await _ensure_partition(db_session, old_month_ts)
    expired_id = await _row(db_session, org_id=org_id, ts=expired_ts, action="boundary-old")
    kept_id = await _row(db_session, org_id=org_id, ts=kept_ts, action="boundary-new")
    old_id = await _row(db_session, org_id=org_id, ts=old_month_ts, action="old-month")

    retention_days = max(1, (now - cutoff).days)
    engine = create_async_engine(os.environ["CAM_DATABASE_URL"])
    maker = create_session_factory(engine)
    try:
        await run_log_retention(maker, Settings(log_retention_days=retention_days))
    finally:
        await engine.dispose()

    assert await _fetch(db_session, expired_id) is None
    assert await _fetch(db_session, kept_id) is not None
    assert await _fetch(db_session, old_id) is None

    remaining = {name for name, _year, _month in await list_partitions(db_session)}
    assert partition_name(kept_ts.year, kept_ts.month) in remaining


async def test_effective_retention_days_used(db_session: AsyncSession) -> None:
    """R23: when the resolver is present, org-configured retention wins."""
    resolver = getattr(retention_module, "_effective_settings", None)
    if resolver is None:
        pytest.skip("effective-settings resolver not present yet")
    org = await ensure_org(db_session)
    original = dict(org.settings)
    try:
        org.settings = {"log_retention_days": 7}
        await db_session.commit()
        days = await retention_module._resolve_retention_days(  # pyright: ignore[reportPrivateUsage]
            db_session, Settings(log_retention_days=30), org_id=org.id
        )
        assert days == 7
    finally:
        org.settings = original
        await db_session.commit()
