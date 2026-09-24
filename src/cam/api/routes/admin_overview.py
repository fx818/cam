from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from cam.api.deps import SessionIdentity, require_admin
from cam.api.queries import admin_overview_stats, list_org_audit_preview

router = APIRouter(prefix="/api/admin/overview", tags=["admin", "admin-overview"])

AdminDep = Annotated[SessionIdentity, Depends(require_admin)]


@router.get("")
async def get_overview(
    request: Request,
    admin: AdminDep,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> dict[str, object]:
    recent = 20 if limit is None else limit
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        stats = await admin_overview_stats(db, admin)
        recent_audit = await list_org_audit_preview(db, admin, limit=recent)
    return {**stats, "recent_audit": recent_audit}
