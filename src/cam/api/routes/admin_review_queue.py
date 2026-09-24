"""Admin review queue routes: list and bulk-approve re-synced new tools."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from cam.api.deps import SessionIdentity, require_admin
from cam.api.errors import ApiProblem
from cam.registry.review_queue import approve_tools, list_pending_tools

router = APIRouter(prefix="/api/admin/review-queue", tags=["admin", "review-queue"])

AdminDep = Annotated[SessionIdentity, Depends(require_admin)]


class ApproveBody(BaseModel):
    tool_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


@router.get("")
async def get_review_queue(request: Request, admin: AdminDep) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        entries = await list_pending_tools(db, org_id=admin.org_id)
    return {"tools": [entry.payload() for entry in entries]}


@router.post("/approve")
async def approve_review_queue_items(
    request: Request, admin: AdminDep, body: ApproveBody
) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        result = await approve_tools(db, org_id=admin.org_id, tool_ids=body.tool_ids)
        await db.commit()
    if not result.approved:
        raise ApiProblem("NOT_FOUND", "No pending tools to approve.", 404)
    return {
        "approved": [str(tool_id) for tool_id in result.approved],
        "unknown": [str(tool_id) for tool_id in result.unknown],
    }
