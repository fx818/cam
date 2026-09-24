import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cam.api.routes.health import router as health_router
from cam.config import Settings
from cam.db.session import create_db_engine


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        cfg = settings if settings is not None else Settings()
        logging.basicConfig(level=cfg.log_level, format="%(levelname)s %(name)s %(message)s")
        engine = create_db_engine(cfg.database_url)
        app.state.settings = cfg
        app.state.engine = engine
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="CAM", lifespan=lifespan)
    app.include_router(health_router)
    return app


app = create_app()
