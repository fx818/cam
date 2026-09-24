"""Admin template registration: `POST /api/admin/services` (Phase 3.6).

Templates are org-wide services every user can reach with a per-user
connection. Body mirrors the dashboard create form; the caller must be an
admin. Kept as a separate file so the Phase 4 merge stays file-level.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from cam.api.deps import SessionIdentity, require_admin
from cam.api.errors import ApiProblem
from cam.registry.services import ServiceAlreadyExists, ServiceError, register_service

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
