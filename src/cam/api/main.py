import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from cam.api.errors import install_error_handlers
from cam.api.routes.connect import router as connect_router
from cam.api.routes.health import router as health_router
from cam.api.routes.services_create import router as services_create_router
from cam.audit.writer import AuditWriter
from cam.config import Settings
from cam.db.session import create_db_engine, create_session_factory
from cam.gateway.auth import BearerAuthMiddleware
from cam.gateway.server import create_mcp_server, load_server_instructions
from cam.invoke.pipeline import GatewayDeps
from cam.invoke.rate_limit import RateLimiter
from cam.upstream.pool import SessionPool


class GatewayMount:
    def __init__(self) -> None:
        self._app: ASGIApp | None = None

    def set(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._app is None:
            body = b'{"error":{"code":"INTERNAL","message":"CAM gateway is starting."}}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self._app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings if settings is not None else Settings()
    engine = create_db_engine(cfg.database_url)
    session_factory = create_session_factory(engine)
    pool = SessionPool(settings=cfg)
    audit = AuditWriter(session_factory)
    deps = GatewayDeps(
        session_factory=session_factory,
        pool=pool,
        settings=cfg,
        rate_limiter=RateLimiter(cfg.rate_limit_per_min),
        audit=audit,
    )
    gateway = GatewayMount()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        logging.basicConfig(level=cfg.log_level, format="%(levelname)s %(name)s %(message)s")
        app.state.settings = cfg
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.pool = pool
        app.state.deps = deps
        app.state.audit = audit
        instructions = await load_server_instructions(session_factory)
        server = create_mcp_server(deps, instructions=instructions)
        _ = server.streamable_http_app(
            streamable_http_path="/mcp",
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        gateway.set(
            BearerAuthMiddleware(StreamableHTTPASGIApp(server.session_manager), session_factory)
        )
        app.state.mcp_server = server
        try:
            await audit.start()
            async with server.session_manager.run():
                yield
        finally:
            await audit.aclose()
            await pool.aclose()
            await engine.dispose()

    app = FastAPI(title="CAM", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(services_create_router)
    app.include_router(connect_router)
    app.router.routes.append(Route("/mcp", endpoint=gateway, methods=["GET", "POST", "DELETE"]))
    return app


app = create_app()
