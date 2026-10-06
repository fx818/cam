from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from cam.access.resolution import ServiceNotFound
from cam.api.deps import SessionIdentity, require_user
from cam.api.errors import ApiProblem
from cam.api.pagination import parse_limit
from cam.api.queries import (
    AuditFilters,
    admin_forensic_export,
    build_audit_csv,
    get_audit_entry,
    list_audit_entries,
)

router = APIRouter(prefix="/api/audit", tags=["audit"])

IdentityDep = Annotated[SessionIdentity, Depends(require_user)]


@router.get("")
async def get_audit(
    request: Request,
    identity: IdentityDep,
    scope: Annotated[Literal["mine", "my_services", "both", "all"], Query()] = "both",
    agent: str | None = None,
    service: str | None = None,
    status: str | None = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    token_id: Annotated[uuid.UUID | None, Query()] = None,
    agent_name: str | None = None,
    q: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> dict[str, object]:
    if scope == "all" and identity.user.role != "admin":
        raise ApiProblem("FORBIDDEN", "Admins only.", 403)
    page_limit = parse_limit(limit)
    filters = AuditFilters(
        scope=scope,
        agent=agent.strip() if agent else None,
        service_ref=service.strip() if service else None,
        status=status.strip() if status else None,
        date_from=date_from,
        date_to=date_to,
        agent_name=agent_name.strip() if agent_name else None,
        token_id=token_id,
        args_query=q.strip() if q else None,
    )
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        entries, next_cursor = await list_audit_entries(
            db, identity, filters, limit=page_limit, cursor=cursor
        )
    return {"entries": entries, "next_cursor": next_cursor}


@router.get("/export")
async def export_audit(
    request: Request,
    identity: IdentityDep,
    agent: str | None = None,
    service: str | None = None,
    status: str | None = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    token_id: Annotated[uuid.UUID | None, Query()] = None,
    agent_name: str | None = None,
    q: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=20000)] = None,
) -> Response:
    if identity.user.role != "admin":
        raise ApiProblem("FORBIDDEN", "Admins only.", 403)
    filters = AuditFilters(
        scope="all",
        agent=agent.strip() if agent else None,
        service_ref=service.strip() if service else None,
        status=status.strip() if status else None,
        date_from=date_from,
        date_to=date_to,
        agent_name=agent_name.strip() if agent_name else None,
        token_id=token_id,
        args_query=q.strip() if q else None,
    )
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        entries = await admin_forensic_export(
            db, identity, filters, max_rows=min(limit or 5000, 20000)
        )
    csv_text, filename = build_audit_csv(entries)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{entry_id}")
async def get_audit_detail(
    request: Request, identity: IdentityDep, entry_id: uuid.UUID
) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            entry = await get_audit_entry(db, identity, entry_id)
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "Activity entry not found.", 404) from None
    return {"entry": entry}
