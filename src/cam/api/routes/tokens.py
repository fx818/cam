from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from cam.api.deps import SessionIdentity, require_user
from cam.api.errors import ApiProblem
from cam.api.pagination import parse_limit
from cam.api.queries import (
    create_user_token,
    list_user_tokens,
    revoke_user_token,
    token_public,
)

router = APIRouter(prefix="/api/tokens", tags=["tokens"])

IdentityDep = Annotated[SessionIdentity, Depends(require_user)]


class TokenCreateBody(BaseModel):
    label: str = Field(min_length=1, max_length=100)


@router.get("")
async def list_tokens(
    request: Request,
    identity: IdentityDep,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> dict[str, object]:
    page_limit = parse_limit(limit)
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        tokens, next_cursor = await list_user_tokens(db, identity, limit=page_limit, cursor=cursor)
        return {"tokens": [token_public(token) for token in tokens], "next_cursor": next_cursor}


@router.post("", status_code=201)
async def create_token_route(
    request: Request, identity: IdentityDep, body: TokenCreateBody
) -> dict[str, object]:
    label = body.label.strip()
    if not label:
        raise ApiProblem("VALIDATION_ERROR", "label must not be empty", 422)
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        raw_token, token = await create_user_token(db, identity, label=label)
        await db.commit()
    payload = token_public(token)
    payload["token"] = raw_token
    return payload


@router.delete("/{token_id}")
async def delete_token(
    request: Request, identity: IdentityDep, token_id: uuid.UUID
) -> dict[str, bool]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            await revoke_user_token(db, identity, token_id)
        except Exception:
            raise ApiProblem("NOT_FOUND", "Token not found.", 404) from None
        await db.commit()
    return {"ok": True}
