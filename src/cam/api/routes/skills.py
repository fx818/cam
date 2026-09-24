from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from cam.api.deps import SessionIdentity, require_user

router = APIRouter(prefix="/api/skills", tags=["skills"])

IdentityDep = Annotated[SessionIdentity, Depends(require_user)]


@router.get("")
async def list_skills(request: Request, identity: IdentityDep) -> dict[str, object]:
    return {"skills": [], "next_cursor": None}
