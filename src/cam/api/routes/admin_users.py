from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from cam.access.resolution import ServiceNotFound
from cam.accounts.users import (
    EmailAlreadyRegistered,
    HandleAlreadyTaken,
    UserError,
    create_user,
    deactivate_user,
)
from cam.api.deps import SessionIdentity, require_admin, user_public
from cam.api.errors import ApiProblem
from cam.api.pagination import parse_limit
from cam.api.queries import (
    admin_user_public,
    count_active_admins,
    get_org_user,
    list_audit_for_user,
    list_org_users,
    list_tokens_for_user,
)
from cam.connections.passwords import hash_password
from cam.db.models import User

router = APIRouter(prefix="/api/admin/users", tags=["admin", "admin-users"])

AdminDep = Annotated[SessionIdentity, Depends(require_admin)]


class AdminUserCreateBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=256)
    role: str | None = Field(default=None, pattern="^(admin|user)$")
    kind: str = Field(default="person", pattern="^(person|team)$")
    display_name: str | None = Field(default=None, min_length=1, max_length=200)


class AdminUserPatchBody(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: str | None = Field(default=None, pattern="^(admin|user)$")
    kind: str | None = Field(default=None, pattern="^(person|team)$")


class ResetPasswordBody(BaseModel):
    password: str = Field(min_length=8, max_length=256)


async def _revoke_user_sessions(db: AsyncSession, user: User) -> None:
    """R17: revoke a user's dashboard sessions after an admin password reset.

    Calls `cam.auth.sessions.invalidate_user_sessions` (bumps the user's
    `session_version`, which `cam.api.deps.load_identity` checks). If that
    contract is ever absent, fall back to bumping the version directly.
    """
    try:
        from cam.auth.sessions import invalidate_user_sessions
    except ImportError:  # pragma: no cover - defensive
        user.session_version = (user.session_version or 0) + 1
        return
    await invalidate_user_sessions(db, user)


@router.get("")
async def list_users(request: Request, admin: AdminDep) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        users = await list_org_users(db, admin)
    return {"users": users}


@router.post("", status_code=201)
async def create_user_route(
    request: Request, admin: AdminDep, body: AdminUserCreateBody
) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            user = await create_user(
                db,
                email=body.email,
                password=body.password,
                role=body.role or "user",  # type: ignore[arg-type]
                kind=body.kind,  # type: ignore[arg-type]
                display_name=body.display_name,
                org_id=admin.org_id,
            )
        except EmailAlreadyRegistered as exc:
            raise ApiProblem("CONFLICT", str(exc), 409) from None
        except HandleAlreadyTaken as exc:
            raise ApiProblem("CONFLICT", str(exc), 409) from None
        except UserError as exc:
            raise ApiProblem("VALIDATION_ERROR", str(exc), 422) from None
        await db.commit()
    return {"user": admin_user_public(user)}


@router.get("/{user_id}")
async def get_user_route(
    request: Request, admin: AdminDep, user_id: uuid.UUID
) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            user = await get_org_user(db, admin, user_id)
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "User not found.", 404) from None
        return {"user": admin_user_public(user)}


@router.patch("/{user_id}")
async def patch_user_route(
    request: Request, admin: AdminDep, user_id: uuid.UUID, body: AdminUserPatchBody
) -> dict[str, object]:
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise ApiProblem("VALIDATION_ERROR", "No fields to update.", 422)
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            user = await get_org_user(db, admin, user_id)
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "User not found.", 404) from None
        if "role" in updates and user.id == admin.user.id and updates["role"] != user.role:
            raise ApiProblem("VALIDATION_ERROR", "You cannot change your own role.", 422)
        if (
            "role" in updates
            and user.role == "admin"
            and updates["role"] == "user"
            and user.status == "active"
        ):
            remaining = await count_active_admins(db, admin, exclude=user.id)
            if remaining == 0:
                raise ApiProblem("VALIDATION_ERROR", "At least one active admin must remain.", 422)
        for field, value in updates.items():
            if value is None:
                # The PATCH body keeps explicit nulls (exclude_unset=True), but
                # role/kind/display_name are NOT NULL columns, so a null would
                # surface as a 500 instead of a 422 (R39).
                raise ApiProblem("VALIDATION_ERROR", f"{field} cannot be null", 422)
            setattr(user, field, value)
        await db.flush()
        await db.commit()
    return {"user": admin_user_public(user)}


@router.post("/{user_id}/reset-password")
async def reset_password_route(
    request: Request, admin: AdminDep, user_id: uuid.UUID, body: ResetPasswordBody
) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            user = await get_org_user(db, admin, user_id)
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "User not found.", 404) from None
        user.password_hash = await hash_password(body.password)
        await _revoke_user_sessions(db, user)
        await db.flush()
        await db.commit()
    return {"user": user_public(user)}


@router.post("/{user_id}/deactivate")
async def deactivate_user_route(
    request: Request, admin: AdminDep, user_id: uuid.UUID
) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    pool = request.app.state.pool
    async with session_factory() as db:
        try:
            user = await get_org_user(db, admin, user_id)
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "User not found.", 404) from None
        if user.id == admin.user.id:
            raise ApiProblem("VALIDATION_ERROR", "You cannot deactivate yourself.", 422)
        if user.role == "admin" and user.status == "active":
            remaining = await count_active_admins(db, admin, exclude=user.id)
            if remaining == 0:
                raise ApiProblem("VALIDATION_ERROR", "At least one active admin must remain.", 422)
        user = await deactivate_user(db, user=user, pool=pool)
        await db.commit()
    return {"user": admin_user_public(user)}


@router.get("/{user_id}/tokens")
async def list_user_tokens_route(
    request: Request, admin: AdminDep, user_id: uuid.UUID
) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            user = await get_org_user(db, admin, user_id)
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "User not found.", 404) from None
        tokens = await list_tokens_for_user(db, admin, user)
    return {"tokens": tokens}


@router.get("/{user_id}/activity")
async def list_user_activity_route(
    request: Request,
    admin: AdminDep,
    user_id: uuid.UUID,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> dict[str, object]:
    page_limit = parse_limit(limit)
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            user = await get_org_user(db, admin, user_id)
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "User not found.", 404) from None
        entries = await list_audit_for_user(db, admin, user, limit=page_limit)
    return {"entries": entries, "next_cursor": None}
