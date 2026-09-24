from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from cam.access.resolution import ServiceNotFound
from cam.api.deps import SessionIdentity, require_user
from cam.api.errors import ApiProblem
from cam.api.queries import (
    apply_service_patch,
    apply_tool_patch,
    connection_public,
    get_tool_context,
    list_dashboard_services,
    list_service_tools,
    lookup_service_context,
    service_public,
    soft_delete_service,
    tool_public,
    viewer_connection,
)
from cam.db.models import Connection

router = APIRouter(prefix="/api", tags=["services"])

IdentityDep = Annotated[SessionIdentity, Depends(require_user)]


class ServicePatchBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    timeout_ms: int | None = Field(default=None, ge=1000, le=600_000)
    response_cap_tokens: int | None = Field(default=None, ge=100, le=1_000_000)
    status: Literal["active", "disabled"] | None = None


class ToolPatchBody(BaseModel):
    enabled: bool | None = None
    description_override: str | None = Field(default=None, max_length=4000)
    tags: list[str] | None = None


@router.get("/services")
async def get_services(request: Request, identity: IdentityDep) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        services = await list_dashboard_services(db, identity)
    return {"services": services}


@router.get("/services/{owner}/{slug}")
async def get_service(
    request: Request, identity: IdentityDep, owner: str, slug: str
) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            context = await lookup_service_context(db, identity, owner, slug)
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "Service not found.", 404) from None
        connection = await viewer_connection(db, context.service, identity)
        return {
            "service": service_public(context.service, context.owner_handle),
            "connection": connection_public(connection),
            "viewer": {
                "is_owner": context.is_owner,
                "is_admin": context.is_admin,
                "can_manage": context.is_admin or context.is_owner,
                "is_shared": not context.is_owner and context.service.tier == "personal",
            },
        }


@router.patch("/services/{owner}/{slug}")
async def patch_service(
    request: Request,
    identity: IdentityDep,
    owner: str,
    slug: str,
    body: ServicePatchBody,
) -> dict[str, object]:
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise ApiProblem("VALIDATION_ERROR", "No fields to update.", 422)
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            context = await lookup_service_context(db, identity, owner, slug)
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "Service not found.", 404) from None
        if not context.can_manage:
            raise ApiProblem("FORBIDDEN", "You cannot change this service.", 403)
        try:
            apply_service_patch(context.service, updates)
        except ValueError as exc:
            raise ApiProblem("VALIDATION_ERROR", str(exc), 422) from None
        await db.commit()
        return {"service": service_public(context.service, context.owner_handle)}


@router.delete("/services/{owner}/{slug}")
async def delete_service(
    request: Request, identity: IdentityDep, owner: str, slug: str
) -> dict[str, bool]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            context = await lookup_service_context(db, identity, owner, slug)
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "Service not found.", 404) from None
        if not context.can_manage:
            raise ApiProblem("FORBIDDEN", "You cannot delete this service.", 403)
        connections = list(
            (
                await db.execute(
                    select(Connection).where(Connection.service_id == context.service.id)
                )
            ).scalars()
        )
        await soft_delete_service(db, context.service)
        await db.commit()
    pool = request.app.state.pool
    for connection in connections:
        await pool.invalidate_connection(str(connection.service_id), str(connection.id))
    return {"ok": True}


@router.get("/services/{owner}/{slug}/tools")
async def get_service_tools(
    request: Request, identity: IdentityDep, owner: str, slug: str
) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            context = await lookup_service_context(db, identity, owner, slug)
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "Service not found.", 404) from None
        tools = await list_service_tools(db, context.service, can_manage=context.can_manage)
        return {
            "tools": [tool_public(tool) for tool in tools],
            "can_manage": context.can_manage,
        }


@router.patch("/tools/{tool_id}")
async def patch_tool(
    request: Request, identity: IdentityDep, tool_id: uuid.UUID, body: ToolPatchBody
) -> dict[str, object]:
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise ApiProblem("VALIDATION_ERROR", "No fields to update.", 422)
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            tool, context = await get_tool_context(db, identity, tool_id)
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "Tool not found.", 404) from None
        if not context.can_manage:
            raise ApiProblem("FORBIDDEN", "You cannot change this service's tools.", 403)
        apply_tool_patch(tool, updates)
        await db.commit()
        return {"tool": tool_public(tool)}


@router.post("/services/{owner}/{slug}/resync")
async def resync_service(
    request: Request, identity: IdentityDep, owner: str, slug: str
) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            context = await lookup_service_context(db, identity, owner, slug)
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "Service not found.", 404) from None
        if not context.can_manage:
            raise ApiProblem("FORBIDDEN", "You cannot re-sync this service.", 403)
    raise ApiProblem("NOT_IMPLEMENTED", "Re-sync arrives with Phase 5.", 501)
