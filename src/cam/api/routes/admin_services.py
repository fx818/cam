"""Admin service registration and visibility management routes.

Templates and org-wide globals are registered via `POST /api/admin/services`.
Visibility is managed per service through `/services/{owner}/{slug}/visibility`
(nullable single-segment `/{slug}` variants are also registered). The caller
must be an admin and the org always comes from the identity.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.access.resolution import GLOBAL_OWNER, service_ref
from cam.api.deps import SessionIdentity, require_admin
from cam.api.errors import ApiProblem
from cam.db.models import Service
from cam.registry.services import (
    ServiceAlreadyExists,
    ServiceError,
    get_admin_service,
    grant_service_visibility,
    list_service_visibility_grantees,
    register_service,
    revoke_service_visibility,
    set_service_visibility,
)

router = APIRouter(prefix="/api/admin", tags=["admin-services"])

AdminDep = Annotated[SessionIdentity, Depends(require_admin)]


class CreateTemplateBody(BaseModel):
    slug: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    transport: Literal["streamable-http", "sse"] | None = None
    auth_type: Literal["none", "static-header", "bearer", "api-key", "basic", "oauth"] = "none"
    header_name: str | None = None
    credential: str | None = None
    description: str = ""
    timeout_ms: int | None = Field(default=None, ge=1000, le=600_000)
    response_cap_tokens: int | None = Field(default=None, ge=100, le=1_000_000)
    kind: Literal["mcp", "rest"] = "mcp"
    spec_url: str | None = Field(default=None, max_length=2000)
    api_key_name: str | None = None
    api_key_in: Literal["header", "query"] | None = None
    username: str | None = None
    tools: list[str] | None = None


@router.post("/services")
async def create_template(
    request: Request, admin: AdminDep, body: CreateTemplateBody
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            service, _connection = await register_service(
                db,
                tier="template",
                slug=body.slug,
                name=body.name,
                url=body.url,
                kind=body.kind,
                transport=body.transport,
                auth_type=body.auth_type,
                header_name=body.header_name,
                credential=body.credential,
                description=body.description,
                spec_url=body.spec_url,
                api_key_name=body.api_key_name,
                api_key_in=body.api_key_in,
                username=body.username,
                timeout_ms=body.timeout_ms,
                response_cap_tokens=body.response_cap_tokens,
                tools=body.tools,
            )
            await db.commit()
        except ServiceAlreadyExists as exc:
            raise ApiProblem("CONFLICT", str(exc), 409) from None
        except ServiceError as exc:
            raise ApiProblem("VALIDATION_ERROR", str(exc), 422) from None
        from cam.api.routes.services_create import service_public

        return {"service": service_public(service, None)}


@router.get("/services")
async def list_admin_services(request: Request, admin: AdminDep) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        result = await db.execute(
            select(Service).where(
                Service.org_id == admin.org_id,
                Service.tier.in_(("global", "template")),
                Service.status != "deleted",
            )
        )
        rows = result.scalars().all()
        from cam.api.routes.services_create import service_public

        return {"services": [service_public(service, None) for service in rows]}


class VisibilityBody(BaseModel):
    visibility: Literal["all", "restricted"]


class VisibilityGrantBody(BaseModel):
    email: str


def _resolve_slug(owner: str) -> None:
    if owner not in ("", GLOBAL_OWNER):
        raise ApiProblem("NOT_FOUND", "Service not found.", 404) from None


def _visibility_problem(exc: ServiceError) -> ApiProblem:
    message = str(exc)
    if (
        message.startswith("no global or template service")
        or message.startswith("no user")
        or "has no visibility grant" in message
    ):
        return ApiProblem("NOT_FOUND", message, 404)
    if "already has visibility" in message:
        return ApiProblem("CONFLICT", message, 409)
    return ApiProblem("VALIDATION_ERROR", message, 422)


async def _visibility_state(db: AsyncSession, slug: str) -> dict[str, Any]:
    service = await get_admin_service(db, slug=slug)
    grantees = await list_service_visibility_grantees(db, service=service)
    return {
        "service": service_ref(service),
        "visibility": service.visibility,
        "grantees": grantees,
    }


async def get_visibility(
    request: Request, admin: AdminDep, slug: str, owner: str = ""
) -> dict[str, Any]:
    _resolve_slug(owner)
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            return await _visibility_state(db, slug)
        except ServiceError as exc:
            raise _visibility_problem(exc) from None


async def patch_visibility(
    request: Request,
    admin: AdminDep,
    slug: str,
    body: VisibilityBody,
    owner: str = "",
) -> dict[str, Any]:
    _resolve_slug(owner)
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            await set_service_visibility(db, slug=slug, visibility=body.visibility)
            await db.commit()
            return await _visibility_state(db, slug)
        except ServiceError as exc:
            await db.rollback()
            raise _visibility_problem(exc) from None


async def change_grant_visibility(
    request: Request,
    admin: AdminDep,
    slug: str,
    body: VisibilityGrantBody,
    owner: str = "",
) -> dict[str, Any]:
    _resolve_slug(owner)
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            if request.method == "DELETE":
                await revoke_service_visibility(db, slug=slug, user_email=body.email)
            else:
                await grant_service_visibility(db, slug=slug, user_email=body.email)
            await db.commit()
            return await _visibility_state(db, slug)
        except ServiceError as exc:
            await db.rollback()
            raise _visibility_problem(exc) from None


async def post_revoke_visibility(
    request: Request,
    admin: AdminDep,
    slug: str,
    body: VisibilityGrantBody,
    owner: str = "",
) -> dict[str, Any]:
    _resolve_slug(owner)
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            await revoke_service_visibility(db, slug=slug, user_email=body.email)
            await db.commit()
            return await _visibility_state(db, slug)
        except ServiceError as exc:
            await db.rollback()
            raise _visibility_problem(exc) from None


def _register_visibility_routes() -> None:
    for base in ("/services/{owner}/{slug}/visibility", "/services/{slug}/visibility"):
        router.add_api_route(base, get_visibility, methods=["GET"])
        router.add_api_route(base, patch_visibility, methods=["PATCH"])
    for base in (
        "/services/{owner}/{slug}/visibility/grant",
        "/services/{slug}/visibility/grant",
    ):
        router.add_api_route(base, change_grant_visibility, methods=["POST", "DELETE"])
    for base in (
        "/services/{owner}/{slug}/visibility/revoke",
        "/services/{slug}/visibility/revoke",
    ):
        router.add_api_route(base, post_revoke_visibility, methods=["POST"])


_register_visibility_routes()
