"""Log retention: keep `audit_logs` 30 days, drop its monthly partitions.

The daily job first makes sure the current and next months have partitions
(future months, so inserts never fail), then drops every partition whose
month already ended past the retention cutoff.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cam.config import Settings
from cam.db.partitions import (
    PARENT_TABLE,
    parse_partition_name,
    partition_bounds,
    partition_name,
)

logger = logging.getLogger(__name__)

MONTHS_AHEAD = 2


async def list_partitions(
    db: AsyncSession, *, parent: str = PARENT_TABLE
) -> list[tuple[str, int, int]]:
    rows = (
        (
            await db.execute(
                text(
                    "SELECT c.relname FROM pg_inherits i "
                    "JOIN pg_class c ON c.oid = i.inhrelid "
                    "WHERE i.inhparent = :parent::regclass"
                ),
                {"parent": parent},
            )
        )
        .scalars()
        .all()
    )
    named: list[tuple[str, int, int]] = []
    for name in rows:
        parsed = parse_partition_name(name, parent=parent)
        if parsed is not None:
            named.append((name, parsed[0], parsed[1]))
    return named


async def ensure_partitions(
    db: AsyncSession, *, months_ahead: int = MONTHS_AHEAD, parent: str = PARENT_TABLE
) -> list[str]:
    now = datetime.now(tz=UTC)
    created: list[str] = []
    for offset in range(0, months_ahead + 1):
        month = now.month + offset
        year = now.year
        while month > 12:
            month -= 12
            year += 1
        name = partition_name(year, month, parent=parent)
        exists = await _exists(db, name)
        if exists:
            continue
        start, end = partition_bounds(year, month)
        await db.execute(
            text(
                f"CREATE TABLE {name} PARTITION OF {parent} "
                f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
            )
        )
        created.append(name)
    return created


async def _exists(db: AsyncSession, name: str) -> bool:
    row = (
        await db.execute(text("SELECT 1 FROM pg_class WHERE relname = :name"), {"name": name})
    ).first()
    return row is not None


async def drop_partitions_older_than(
    db: AsyncSession, cutoff: datetime, *, parent: str = PARENT_TABLE
) -> list[str]:
    dropped: list[str] = []
    for name, year, month in await list_partitions(db, parent=parent):
        _start, end = partition_bounds(year, month)
        if end <= cutoff:
            await db.execute(text(f"DROP TABLE IF EXISTS {name}"))
            dropped.append(name)
    return dropped


async def run_log_retention(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
) -> int:
    cfg = settings if settings is not None else Settings()
    retention_days = cfg.log_retention_days
    cutoff = datetime.now(tz=UTC) - timedelta(days=retention_days)
    created: list[str] = []
    dropped: list[str] = []
    async with session_factory() as db:
        created = await ensure_partitions(db)
        dropped = await drop_partitions_older_than(db, cutoff)
        await db.commit()
    if created:
        logger.info("created audit partitions: %s", ", ".join(created))
    if dropped:
        logger.info("dropped audit partitions: %s", ", ".join(dropped))
    return len(dropped)
