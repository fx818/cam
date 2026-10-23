"""R13 regression: upgrading a *populated* audit_logs table to month partitions.

The partition migration (b8d2f1a6c4e7) used to insert existing rows into the new
partitioned table before creating any child partitions, which raised
`no partition of relation "audit_logs_new" found for row`. This test stages a
real upgrade from the previous revision with rows spanning several months and
asserts every row survives into the correct partition.
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime

import alembic.command
import alembic.config
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from cam.db.partitions import partition_name

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
R13_TEST_URL = "postgresql+asyncpg://cam:cam@localhost:5432/cam_pr_r13_upgrade_test"
PREVIOUS_REVISION = "e8a1c4f27b93"


def _database_name(url: str) -> tuple[str, str]:
    base, _, name = url.rpartition("/")
    return base, name


def _month_back(offset: int) -> datetime:
    now = datetime.now(tz=UTC)
    month = now.month - offset
    year = now.year
    while month < 1:
        month += 12
        year -= 1
    return datetime(year, month, 15, 12, tzinfo=UTC)


async def _recreate_database(url: str) -> None:
    base, name = _database_name(url)
    engine = create_async_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


async def _insert_audit_rows(url: str) -> list[datetime]:
    timestamps = [_month_back(0), _month_back(1), _month_back(3)]
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            org_id = (
                await conn.execute(text("SELECT id FROM orgs WHERE slug = 'indiamart'"))
            ).scalar_one()
            for ts in timestamps:
                await conn.execute(
                    text(
                        "INSERT INTO audit_logs (id, org_id, ts, action) "
                        "VALUES (:id, :org_id, :ts, :action)"
                    ),
                    {"id": uuid.uuid4(), "org_id": org_id, "ts": ts, "action": f"r13-{ts.month}"},
                )
    finally:
        await engine.dispose()
    return timestamps


async def _count_rows(url: str) -> int:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return int((await conn.execute(text("SELECT count(*) FROM audit_logs"))).scalar_one())
    finally:
        await engine.dispose()


async def _partitions(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT c.relname FROM pg_inherits i "
                    "JOIN pg_class c ON c.oid = i.inhrelid "
                    "JOIN pg_class p ON p.oid = i.inhparent "
                    "WHERE p.relname = 'audit_logs'"
                )
            )
            return {row[0] for row in rows}
    finally:
        await engine.dispose()


def test_upgrade_populated_audit_logs_creates_partitions_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_recreate_database(R13_TEST_URL))
    monkeypatch.setenv("CAM_DATABASE_URL", R13_TEST_URL)
    cfg = alembic.config.Config(os.path.join(REPO_ROOT, "alembic.ini"))

    alembic.command.upgrade(cfg, PREVIOUS_REVISION)
    timestamps = asyncio.run(_insert_audit_rows(R13_TEST_URL))
    assert asyncio.run(_count_rows(R13_TEST_URL)) == len(timestamps)

    # This is the step that previously failed on a populated database.
    alembic.command.upgrade(cfg, "head")

    assert asyncio.run(_count_rows(R13_TEST_URL)) == len(timestamps)
    partitions = asyncio.run(_partitions(R13_TEST_URL))
    expected = {partition_name(ts.year, ts.month) for ts in timestamps}
    assert expected <= partitions
    now = datetime.now(tz=UTC)
    assert partition_name(now.year, now.month) in partitions
