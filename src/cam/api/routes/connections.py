from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from cam.access.resolution import ServiceNotFound
from cam.access.shares import ShareError
from cam.api.deps import SessionIdentity, require_user
from cam.api.errors import ApiProblem
from cam.api.queries import (
    add_connection_share,
    get_owned_connection,
    list_connection_shares,
    revoke_share_by_id,
    rotate_connection_secret,
    share_public,
)
from cam.db.models import User

router = APIRouter(prefix="/api", tags=["connections", "shares"])

IdentityDep = Annotated[SessionIdentity, Depends(require_user)]


class RotateBody(BaseModel):
    secret: str = Field(min_length=1, max_length=8192)


class ShareCreateBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)


def _share_problem(exc: ShareError) -> ApiProblem:
    message = str(exc)
    if message == "connection not found" or message == "share not found":
        return ApiProblem("NOT_FOUND", "Not found.", 404)
    return ApiProblem("BAD_REQUEST", message, 400)


@router.post("/connections/{connection_id}/rotate")
async def rotate_connection(
    request: Request, identity: IdentityDep, connection_id: uuid.UUID, body: RotateBody
) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            connection = await get_owned_connection(db, identity, connection_id)
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "Connection not found.", 404) from None
        secret = body.secret.strip()
        if not secret:
            raise ApiProblem("VALIDATION_ERROR", "secret must not be empty", 422)
        await rotate_connection_secret(db, connection, secret=secret)
        await db.commit()
    pool = request.app.state.pool
    await pool.invalidate_connection(str(connection.service_id), str(connection.id))
    return {
        "connection": {
            "id": str(connection.id),
            "status": connection.status,
            "expires_at": None,
        }
    }


@router.get("/connections/{connection_id}/shares")
async def get_shares(
    request: Request, identity: IdentityDep, connection_id: uuid.UUID
) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            connection = await get_owned_connection(db, identity, connection_id)
            shares = await list_connection_shares(db, identity, connection)
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "Connection not found.", 404) from None
        except ShareError as exc:
            raise _share_problem(exc) from None
    return {"shares": shares}


@router.post("/connections/{connection_id}/shares", status_code=201)
async def create_share(
    request: Request,
    identity: IdentityDep,
    connection_id: uuid.UUID,
    body: ShareCreateBody,
) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            connection = await get_owned_connection(db, identity, connection_id)
            share = await add_connection_share(
                db, identity, connection, email=body.email.strip().lower()
            )
            grantee = await db.get(User, share.grantee_user_id)
            await db.commit()
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "Connection not found.", 404) from None
        except ShareError as exc:
            raise _share_problem(exc) from None
    return {"share": share_public(share, grantee)}


@router.delete("/shares/{share_id}")
async def delete_share(
    request: Request, identity: IdentityDep, share_id: uuid.UUID
) -> dict[str, bool]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            await revoke_share_by_id(db, identity, share_id)
        except ShareError as exc:
            raise _share_problem(exc) from None
        await db.commit()
    return {"ok": True}
