"""Health endpoints.

``/api/health/live`` is a dependency-free liveness check (the process is up);
it never touches the database, so a DB wobble cannot make Kubernetes restart a
healthy pod. ``/api/health`` (and its alias ``/api/health/ready``) is the
readiness check: it depends on the database and returns 503 while the DB is
unreachable, so the pod is pulled out of the load balancer but not restarted.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cam.db.session import ping

router = APIRouter(tags=["health"])


@router.get("/api/health/live")
async def liveness() -> JSONResponse:
    """Liveness: process is running. No external dependencies."""
    return JSONResponse({"status": "ok"})


@router.get("/api/health")
async def readiness(request: Request) -> JSONResponse:
    engine = request.app.state.engine
    if await ping(engine):
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "unhealthy"}, status_code=503)


@router.get("/api/health/ready")
async def readiness_alias(request: Request) -> JSONResponse:
    return await readiness(request)
