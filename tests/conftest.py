import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import alembic.command
import alembic.config
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from cam.db.session import create_db_engine, create_session_factory

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fixtures.mock_mcp import MockMCPHandle, start_mock_mcp  # noqa: E402

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


async def _recreate_database(url: str) -> None:
    base, name = _database_name(url)
    engine = create_async_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


def _upgrade_head() -> None:
    cfg = alembic.config.Config(str(REPO_ROOT / "alembic.ini"))
    alembic.command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
async def migrated_test_database() -> None:
    await _recreate_database(TEST_DATABASE_URL)
    await asyncio.to_thread(_upgrade_head)


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_db_engine(TEST_DATABASE_URL)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture(scope="session")
async def mock_mcp() -> AsyncIterator[MockMCPHandle]:
    """One shared mock for the whole session.

    Starting/stopping the mock more than twice in one process trips a
    streamable-HTTP/SSE teardown race (third initialize never completes),
    so tests share a single long-lived instance and reset counters instead.
    """
    async with start_mock_mcp() as handle:
        yield handle


@pytest.fixture
async def fresh_counts(mock_mcp: MockMCPHandle) -> MockMCPHandle:
    mock_mcp.call_counts.clear()
    return mock_mcp


@pytest.fixture(scope="session")
async def mock_mcp_authed() -> AsyncIterator[MockMCPHandle]:
    async with start_mock_mcp(require_token="test-secret") as handle:
        yield handle
