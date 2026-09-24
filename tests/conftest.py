import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import alembic.command
import alembic.config
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from cam.db.session import create_db_engine, create_session_factory

REPO_ROOT = Path(__file__).resolve().parent.parent

TEST_DATABASE_URL = os.environ.get(
    "CAM_TEST_DATABASE_URL", "postgresql+asyncpg://cam:cam@localhost:5432/cam_test"
)

os.environ["CAM_DATABASE_URL"] = TEST_DATABASE_URL
os.environ["CAM_ENV"] = "dev"
os.environ.setdefault("CAM_ENCRYPTION_KEYS", "v1:AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
os.environ.setdefault("CAM_ENCRYPTION_KEY_CURRENT", "v1")


def _database_name(url: str) -> tuple[str, str]:
    base, _, name = url.rpartition("/")
    return base, name


async def _ensure_database(url: str) -> None:
    base, name = _database_name(url)
    engine = create_async_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            )
            if result.first() is None:
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


def _upgrade_head() -> None:
    cfg = alembic.config.Config(str(REPO_ROOT / "alembic.ini"))
    alembic.command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
async def migrated_test_database() -> None:
    await _ensure_database(TEST_DATABASE_URL)
    await asyncio.to_thread(_upgrade_head)


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_db_engine(TEST_DATABASE_URL)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()
