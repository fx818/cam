"""Log retention: keep `audit_logs` for the retention window, drop its
monthly partitions.

The daily job first makes sure the current and next months have partitions
(future months, so inserts never fail). It then drops every partition whose
month already ended past the retention cutoff, and finally deletes any expired
rows still sitting in the boundary partition (the month straddling the cutoff),
via an indexed `ts` predicate — otherwise rows older than the policy but in the
current boundary month would linger for up to a month.
"""

from __future__ import annotations

import importlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cam.config import Settings
from cam.db.models import Org
from cam.db.partitions import (
    PARENT_TABLE,
    parse_partition_name,
    partition_bounds,
    partition_name,
)


def _load_effective_settings() -> Any:
    """R23: load the shared effective-settings resolver if it has landed.

    Dynamically imported so this job keeps working with the configured
    `Settings` until `cam.api.settings_resolver` exists.
    TODO(r23): drop the `Settings` fallback once the resolver is present.
    """
    try:
        module = importlib.import_module("cam.api.settings_resolver")
    except ImportError:  # pragma: no cover - resolver lands in a parallel wave
        return None
    return getattr(module, "effective_settings", None)


_effective_settings: Any = _load_effective_settings()

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
                    f"WHERE i.inhparent = '{parent}'::regclass"
                )
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


async def delete_expired_rows(
    db: AsyncSession, cutoff: datetime, *, parent: str = PARENT_TABLE
) -> int:
    """R24: delete rows older than ``cutoff`` from the boundary partition.

    Runs after fully-expired partitions are dropped, so the only rows left
    below the cutoff live in the boundary month. Uses the `ix_audit_logs_ts`
    index (`ts < :cutoff`) and relies on partition pruning to touch just that
    partition. Returns the number of rows deleted.
    """
    result = cast(
        "CursorResult[Any]",
        await db.execute(text(f"DELETE FROM {parent} WHERE ts < :cutoff"), {"cutoff": cutoff}),
    )
    return int(result.rowcount or 0)


async def _resolve_retention_days(db: AsyncSession, cfg: Settings, *, org_id: Any = None) -> int:
    """Effective retention days (R23), or the configured default.

    Partitions are global, so if orgs differ we keep the longest retention any
    org requires (never delete data too early for an org that asked for more).
    """
    if _effective_settings is None:
        return cfg.log_retention_days
    try:
        if org_id is not None:
            org_ids = [org_id]
        else:
            org_ids = list((await db.execute(select(Org.id))).scalars())
        days: list[int] = []
        for resolved_org in org_ids:
            effective = await _effective_settings(db, resolved_org)
            value = getattr(effective, "log_retention_days", None)
            if value is not None:
                days.append(int(value))
        return max(days) if days else cfg.log_retention_days
    except Exception:
        logger.warning("effective settings unavailable; using configured retention", exc_info=True)
        return cfg.log_retention_days


async def run_log_retention(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
    *,
    org_id: Any = None,
) -> int:
    cfg = settings if settings is not None else Settings()
    created: list[str] = []
    dropped: list[str] = []
    deleted = 0
    async with session_factory() as db:
        retention_days = await _resolve_retention_days(db, cfg, org_id=org_id)
        cutoff = datetime.now(tz=UTC) - timedelta(days=retention_days)
        created = await ensure_partitions(db)
        dropped = await drop_partitions_older_than(db, cutoff)
        deleted = await delete_expired_rows(db, cutoff)
        await db.commit()
    if created:
        logger.info("created audit partitions: %s", ", ".join(created))
    if dropped:
        logger.info("dropped audit partitions: %s", ", ".join(dropped))
    if deleted:
        logger.info("deleted %d expired audit rows from the boundary partition", deleted)
    return len(dropped)
