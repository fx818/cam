import logging
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from cam.api.csrf import CsrfMiddleware
from cam.api.errors import install_error_handlers
from cam.api.metrics import SCHEDULER_JOB_ERRORS, SCHEDULER_JOB_LAST_RUN, RequestMetricsMiddleware
from cam.api.routes.admin_diagnostics import router as admin_diagnostics_router
from cam.api.routes.admin_overview import router as admin_overview_router
from cam.api.routes.admin_review_queue import router as admin_review_queue_router
from cam.api.routes.admin_services import router as admin_services_router
from cam.api.routes.admin_settings import router as admin_settings_router
from cam.api.routes.admin_users import router as admin_users_router
from cam.api.routes.audit import router as audit_router
from cam.api.routes.auth import router as auth_router
from cam.api.routes.connect import router as connect_router
from cam.api.routes.connections import router as connections_router
from cam.api.routes.health import router as health_router
from cam.api.routes.metrics import router as metrics_router
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

# R04: transport clients log the full request URL — including credential query
# params such as `?token=` — at INFO. Scrub every log record as defence in depth
# and hold those loggers at WARNING or above.
_CREDENTIAL_QUERY_RE = re.compile(
    r"(?i)([?&](?:access_token|refresh_token|id_token|api[_-]?key|apikey|token|secret"
    r"|password|passwd|pwd|credential|signature|sig|code|auth|key)=)[^&#\s\"']+"
)
_AUTH_HEADER_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_TRANSPORT_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2", "urllib3", "hpack")


def redact_credentials(text: str) -> str:
    """Blank credential values in a log line: query-string secrets and
    Authorization header values."""
    text = _CREDENTIAL_QUERY_RE.sub(r"\1***", text)
    return _AUTH_HEADER_RE.sub(r"\1 ***", text)


class CredentialRedactionFilter(logging.Filter):
    """Scrub credentials from every record before it reaches any handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact_credentials(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(cfg: Settings) -> None:
    """Install root logging with a credential-redaction filter and silence the
    HTTP-client loggers that would otherwise print request URLs at INFO."""
    logging.basicConfig(level=cfg.log_level, format="%(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(existing, CredentialRedactionFilter) for existing in handler.filters):
            handler.addFilter(CredentialRedactionFilter())
    configured = getattr(logging, cfg.log_level, logging.INFO)
    transport_level = max(configured, logging.WARNING)
    for name in _TRANSPORT_LOGGERS:
        logging.getLogger(name).setLevel(transport_level)
    # uvicorn installs its own access handler with propagate=False, so the root
    # filter never sees those records (R04) — e.g. an OAuth `?code=`/`?state=`
    # URL would still be logged. Attach the redaction filter to it directly.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        for handler in uvicorn_logger.handlers:
            if not any(
                isinstance(existing, CredentialRedactionFilter) for existing in handler.filters
            ):
                handler.addFilter(CredentialRedactionFilter())


def transport_security_settings(cfg: Settings) -> TransportSecuritySettings:
    """Host/Origin (DNS-rebinding) protection for the /mcp endpoint.

    Enabled in prod, with allowed hosts derived from ``CAM_BASE_URL`` (plus the
    loopback names so in-cluster probes still pass). Left permissive in dev so
    local agents can connect over any hostname.
    """
    if cfg.env != "prod":
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    host = urlsplit(cfg.base_url).hostname or ""
    allowed_hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    allowed_origins: list[str] = []
    if host:
        allowed_hosts[:0] = [host, f"{host}:*"]
        allowed_origins = [f"https://{host}", f"http://{host}"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def instrumented_job(name: str, fn: Callable[[], Awaitable[Any]]) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        try:
            await fn()
        except Exception:
            SCHEDULER_JOB_ERRORS.labels(name).inc()
            logging.getLogger(__name__).exception("background job %s failed", name)
        finally:
            SCHEDULER_JOB_LAST_RUN.labels(name).set(time.time())

    return run


def instrumented_jobs(cfg: Settings, session_factory: Any) -> dict[str, Callable[[], Any]]:
    return {
        "make_health_job": lambda: instrumented_job(
            "health-check", lambda: run_health_checks(session_factory)
        ),
        "make_resync_job": lambda: instrumented_job(
            "resync", lambda: run_resync_all(session_factory)
        ),
        "make_retention_job": lambda: instrumented_job(
            "log-retention", lambda: run_log_retention(session_factory, cfg)
        ),
    }


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
    engine = create_db_engine(
        cfg.database_url,
        pool_size=cfg.db_pool_size,
        max_overflow=cfg.db_max_overflow,
    )
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
    jobs = instrumented_jobs(cfg, session_factory)
    runner = JobRunner(
        cfg,
        session_factory,
        make_health_job=jobs["make_health_job"],
        make_resync_job=jobs["make_resync_job"],
        make_retention_job=jobs["make_retention_job"],
    )
    gateway = GatewayMount()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        configure_logging(cfg)
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
            transport_security=transport_security_settings(cfg),
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
    app.add_middleware(RequestMetricsMiddleware)
    app.state.auth_provider = default_auth_provider()
    app.include_router(auth_router)
    app.include_router(health_router)
    app.include_router(metrics_router)
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
    app.include_router(admin_review_queue_router)
    app.include_router(admin_diagnostics_router)
    app.router.routes.append(Route("/mcp", endpoint=gateway, methods=["GET", "POST", "DELETE"]))
    mount_web(app)
    return app


app = create_app()
