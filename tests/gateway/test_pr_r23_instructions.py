"""R23 (consume): gateway server instructions prefer effective settings, and the
legacy fallback honours the dashboard's `instructions` key."""

from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from cam.connections.orgs import ensure_org
from cam.db.session import create_session_factory
from cam.gateway.server import SERVER_INSTRUCTIONS, load_server_instructions


async def test_load_server_instructions_falls_back_to_dashboard_key(
    db_session: AsyncSession,
) -> None:
    org = await ensure_org(db_session)
    original = dict(org.settings)
    engine = create_async_engine(os.environ["CAM_DATABASE_URL"])
    maker = create_session_factory(engine)
    try:
        org.settings = {"instructions": "DASHBOARD INSTRUCTIONS"}
        await db_session.commit()
        assert await load_server_instructions(maker) == "DASHBOARD INSTRUCTIONS"

        org.settings = {}
        await db_session.commit()
        assert await load_server_instructions(maker) == SERVER_INSTRUCTIONS
    finally:
        org.settings = original
        await db_session.commit()
        await engine.dispose()
