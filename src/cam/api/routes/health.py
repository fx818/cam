from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cam.db.session import ping

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health(request: Request) -> JSONResponse:
    engine = request.app.state.engine
    if await ping(engine):
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "unhealthy"}, status_code=503)
