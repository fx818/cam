import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from cam.api.csrf import CsrfMiddleware
from cam.api.errors import install_error_handlers
from cam.api.routes.admin_overview import router as admin_overview_router
from cam.api.routes.admin_services import router as admin_services_router
from cam.api.routes.admin_settings import router as admin_settings_router
from cam.api.routes.admin_users import router as admin_users_router
from cam.api.routes.audit import router as audit_router
from cam.api.routes.auth import router as auth_router
from cam.api.routes.connect import router as connect_router
from cam.api.routes.connections import router as connections_router
from cam.api.routes.health import router as health_router
from cam.api.routes.services import router as services_router
from cam.api.routes.services_create import router as services_create_router
from cam.api.routes.skills import router as skills_router
from cam.api.routes.tokens import router as tokens_router
from cam.audit.writer import AuditWriter
from cam.auth import default_auth_provider
from cam.config import Settings
from cam.db.session import create_db_engine, create_session_factory
from cam.gateway.auth import BearerAuthMiddleware
from cam.gateway.server import create_mcp_server, load_server_instructions
from cam.invoke.pipeline import GatewayDeps
from cam.invoke.rate_limit import RateLimiter
from cam.jobs.health import run_health_checks
from cam.jobs.resync import run_resync_all
from cam.jobs.retention import run_log_retention
from cam.jobs.runner import JobRunner
from cam.registry.resync import ResyncCoordinator
from cam.upstream.pool import SessionPool


def list_changed_handler_factory(
    coordinator: ResyncCoordinator,
) -> Callable[[str, str], Any]:
    def factory(service_id: str, connection_id: str) -> Any:
        del connection_id

        async def handler(message: object) -> None:
            if type(message).__name__ == "ToolListChangedNotification":
                coordinator.enqueue(service_id)

        return handler

    return factory


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


WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"


async def _safe_job(coroutine_factory: Any) -> None:
    try:
        await coroutine_factory
    except Exception:
        logging.getLogger(__name__).exception("background job failed")


def mount_web(app: FastAPI) -> None:
    index = WEB_DIST / "index.html"
    if not index.is_file():
        return
    root = WEB_DIST.resolve()
    assets = WEB_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    async def spa(full_path: str) -> FileResponse | JSONResponse:
        if full_path == "mcp" or full_path.startswith(("api/", "api", "mcp/")):
            return JSONResponse(
                {"error": {"code": "NOT_FOUND", "message": "Not found."}}, status_code=404
            )
        if full_path:
            candidate = (WEB_DIST / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate)
        return FileResponse(index)


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings if settings is not None else Settings()
    engine = create_db_engine(cfg.database_url)
    session_factory = create_session_factory(engine)
    pool = SessionPool(settings=cfg)
    audit = AuditWriter(session_factory)
    coordinator = ResyncCoordinator(session_factory)
    deps = GatewayDeps(
        session_factory=session_factory,
        pool=pool,
        settings=cfg,
        rate_limiter=RateLimiter(cfg.rate_limit_per_min),
        audit=audit,
        message_handler_factory=list_changed_handler_factory(coordinator),
    )
    runner = JobRunner(
        cfg,
        session_factory,
        make_health_job=lambda: lambda: _safe_job(run_health_checks(session_factory)),
        make_resync_job=lambda: lambda: _safe_job(run_resync_all(session_factory)),
        make_retention_job=lambda: lambda: _safe_job(run_log_retention(session_factory, cfg)),
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
        app.state.resync_coordinator = coordinator
        app.state.job_runner = runner
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
            coordinator.start()
            runner.start()
            async with server.session_manager.run():
                yield
        finally:
            await runner.aclose()
            await coordinator.aclose()
            await audit.aclose()
            await pool.aclose()
            await engine.dispose()

    app = FastAPI(title="CAM", lifespan=lifespan)
    install_error_handlers(app)
    app.add_middleware(CsrfMiddleware)
    app.state.auth_provider = default_auth_provider()
    app.include_router(auth_router)
    app.include_router(health_router)
    app.include_router(tokens_router)
    app.include_router(services_router)
    app.include_router(services_create_router)
    app.include_router(connect_router)
    app.include_router(connections_router)
    app.include_router(audit_router)
    app.include_router(skills_router)
    app.include_router(admin_users_router)
    app.include_router(admin_settings_router)
    app.include_router(admin_services_router)
    app.include_router(admin_overview_router)
    app.router.routes.append(Route("/mcp", endpoint=gateway, methods=["GET", "POST", "DELETE"]))
    mount_web(app)
    return app


app = create_app()
