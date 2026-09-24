import os

from sqlalchemy import delete, inspect, text

from cam.db.base import Setting
from cam.db.session import create_db_engine, create_session_factory

TEST_DATABASE_URL = os.environ["CAM_DATABASE_URL"]


async def test_migrations_created_schema() -> None:
    engine = create_db_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
        assert {"settings", "alembic_version"} <= tables
    finally:
        await engine.dispose()


async def test_alembic_version_recorded() -> None:
    engine = create_db_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            assert result.scalar_one()
    finally:
        await engine.dispose()


async def test_settings_row_roundtrip() -> None:
    engine = create_db_engine(TEST_DATABASE_URL)
    try:
        sessions = create_session_factory(engine)
        async with sessions() as session:
            await session.execute(delete(Setting).where(Setting.key == "phase0_test"))
            session.add(Setting(key="phase0_test", value={"timeout": 60}))
            await session.commit()
        async with sessions() as session:
            loaded = await session.get(Setting, "phase0_test")
            assert loaded is not None
            assert loaded.value == {"timeout": 60}
            await session.delete(loaded)
            await session.commit()
    finally:
        await engine.dispose()
