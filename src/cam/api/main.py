import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cam.api.routes.health import router as health_router
from cam.db.session import create_db_engine

DEV_DATABASE_URL = "postgresql+asyncpg://cam:cam@localhost:5432/cam"


def resolve_database_url(database_url: str | None = None) -> str:
    return database_url or os.environ.get("CAM_DATABASE_URL") or DEV_DATABASE_URL


def create_app(database_url: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        engine = create_db_engine(resolve_database_url(database_url))
        app.state.engine = engine
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="CAM", lifespan=lifespan)
    app.include_router(health_router)
    return app


app = create_app()
