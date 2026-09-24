from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request

from cam.access.resolution import ServiceNotFound
from cam.api.deps import SessionIdentity, require_user
from cam.api.errors import ApiProblem
from cam.api.pagination import parse_limit
from cam.api.queries import AuditFilters, get_audit_entry, list_audit_entries

router = APIRouter(prefix="/api/audit", tags=["audit"])

IdentityDep = Annotated[SessionIdentity, Depends(require_user)]


@router.get("")
async def get_audit(
    request: Request,
    identity: IdentityDep,
    scope: Annotated[Literal["mine", "my_services", "both"], Query()] = "both",
    agent: str | None = None,
    service: str | None = None,
    status: str | None = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> dict[str, object]:
    page_limit = parse_limit(limit)
    filters = AuditFilters(
        scope=scope,
        agent=agent.strip() if agent else None,
        service_ref=service.strip() if service else None,
        status=status.strip() if status else None,
        date_from=date_from,
        date_to=date_to,
    )
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        entries, next_cursor = await list_audit_entries(
            db, identity, filters, limit=page_limit, cursor=cursor
        )
    return {"entries": entries, "next_cursor": next_cursor}


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
