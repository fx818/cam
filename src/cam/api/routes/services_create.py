"""Service creation and testing routes owned by Phase 3.

`POST /api/services` registers a personal service for the caller;
`POST /api/services/test` probes an unsaved definition and returns the tool
list without persisting anything; `POST /api/services/{owner}/{slug}/tools`
defines a manual REST tool. Kept separate from Phase 4's `routes/services.py`
so the merge stays a clean file-level join.
"""

from __future__ import annotations

import base64
import uuid
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.access.resolution import GLOBAL_OWNER, has_access, service_ref
from cam.api.deps import SessionIdentity, require_user
from cam.api.errors import ApiProblem
from cam.connections.tokens import AuthenticatedIdentity
from cam.connections.url_policy import load_url_policy
from cam.db.models import CamToken, Service, Tool, User
from cam.registry.openapi import OpenAPIError, preview_openapi
from cam.registry.rest_tools import RestToolError, define_rest_tool
from cam.registry.services import ServiceAlreadyExists, ServiceError, register_service
from cam.upstream import ConnectionTarget, UpstreamSession
from cam.upstream.errors import UpstreamError

router = APIRouter(prefix="/api", tags=["services-create"])

IdentityDep = Annotated[SessionIdentity, Depends(require_user)]


class CreateServiceBody(BaseModel):
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


class DefineToolBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    method: str = Field(min_length=1, max_length=10)
    path: str = Field(min_length=1, max_length=2000)
    params: dict[str, str] | None = None
    description: str = Field(default="", max_length=4000)
    input_schema: dict[str, Any] | None = None


def service_public(service: Service, owner_handle: str | None) -> dict[str, Any]:
    return {
        "id": service_ref(service, owner_handle),
        "tier": service.tier,
        "name": service.name,
        "description": service.description,
        "slug": service.slug,
        "kind": service.kind,
        "url": service.url,
        "transport": service.transport,
        "spec_url": service.spec_url,
        "auth_type": service.auth_type,
        "auth_config": service.auth_config,
        "visibility": service.visibility,
        "timeout_ms": service.timeout_ms,
        "response_cap_tokens": service.response_cap_tokens,
        "status": service.status,
        "health": service.health,
        "last_synced_at": service.last_synced_at.isoformat() if service.last_synced_at else None,
        "created_at": service.created_at.isoformat() if service.created_at else None,
        "owner": owner_handle,
    }


def tool_public(tool: Tool) -> dict[str, Any]:
    return {
        "id": str(tool.id),
        "name": tool.name,
        "upstream_name": tool.upstream_name,
        "description": tool.description,
        "description_override": tool.description_override,
        "tags": tool.tags,
        "enabled": tool.enabled,
        "status": tool.status,
    }


def _viewer_identity(identity: SessionIdentity) -> AuthenticatedIdentity:
    return AuthenticatedIdentity(
        org_id=identity.org_id,
        user=identity.user,
        token=CamToken(
            org_id=identity.org_id,
            user_id=identity.user.id,
            label="dashboard",
            token_hash="dashboard-session",
            token_prefix="dashboard",
        ),
    )


async def _owner_handle_of(session: AsyncSession, service: Service) -> str | None:
    if service.owner_user_id is None:
        return None
    owner = await session.get(User, service.owner_user_id)
    return owner.handle if owner else None


def _test_credential_material(body: CreateServiceBody) -> tuple[dict[str, str], dict[str, str]]:
    headers: dict[str, str] = {}
    query: dict[str, str] = {}
    credential = body.credential or ""
    if body.auth_type == "static-header":
        header_name = (body.header_name or "").strip()
        if not header_name:
            raise ApiProblem("VALIDATION_ERROR", "static-header auth requires a header name", 422)
        headers[header_name] = credential
    elif body.auth_type == "bearer":
        headers["Authorization"] = f"Bearer {credential}"
    elif body.auth_type == "api-key":
        key_name = (body.api_key_name or "").strip()
        if not key_name:
            raise ApiProblem("VALIDATION_ERROR", "api-key auth requires a key name", 422)
        if (body.api_key_in or "header") == "query":
            query[key_name] = credential
        else:
            headers[key_name] = credential
    elif body.auth_type == "basic":
        username = (body.username or "").strip()
        if not username:
            raise ApiProblem("VALIDATION_ERROR", "basic auth requires a username", 422)
        token = base64.b64encode(f"{username}:{credential}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    return headers, query


async def _test_mcp(
    request: Request, identity: SessionIdentity, body: CreateServiceBody
) -> dict[str, Any]:
    if body.auth_type == "oauth":
        return {
            "ok": False,
            "tools": [],
            "message": "OAuth services are tested after you connect them.",
        }
    headers, query = _test_credential_material(body)
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        policy = await load_url_policy(db, org_id=identity.org_id, restricted=True)
    url = body.url
    if query:
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}{urlencode(query)}"
    target = ConnectionTarget(
        service_id=str(uuid.uuid4()),
        connection_id=str(uuid.uuid4()),
        url=url,
        transport=body.transport or "streamable-http",
        headers=headers,
        url_policy=policy,
    )
    try:
        upstream = UpstreamSession(target, call_timeout_s=15.0)
        await upstream.start()
        try:
            tools = await upstream.list_tools(timeout_s=15.0)
        finally:
            await upstream.aclose()
    except UpstreamError as exc:
        return {"ok": False, "tools": [], "message": exc.message}
    except Exception:
        return {"ok": False, "tools": [], "message": "Could not reach the endpoint."}
    return {
        "ok": True,
        "tools": [{"name": tool.name, "description": tool.description} for tool in tools],
    }


async def _test_rest(
    request: Request, identity: SessionIdentity, body: CreateServiceBody
) -> dict[str, Any]:
    if body.auth_type == "oauth":
        return {
            "ok": False,
            "tools": [],
            "message": "OAuth services are tested after you connect them.",
        }
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        policy = await load_url_policy(db, org_id=identity.org_id, restricted=True)
    spec_location = (body.spec_url or body.url).strip()
    try:
        operations = await preview_openapi(spec_location, url_policy=policy)
    except OpenAPIError as exc:
        return {"ok": False, "tools": [], "message": str(exc)}
    except Exception:
        return {"ok": False, "tools": [], "message": "Could not fetch the OpenAPI spec."}
    return {
        "ok": True,
        "tools": [{"name": op.name, "description": op.description} for op in operations],
    }


async def _lookup_manageable(
    session: AsyncSession, identity: SessionIdentity, owner: str, slug: str
) -> Service:
    owner_part = owner.strip().lower()
    resolved_slug = slug.strip()
    service: Service | None = None
    if owner_part == GLOBAL_OWNER:
        result = await session.execute(
            select(Service).where(
                Service.org_id == identity.org_id,
                Service.tier.in_(("global", "template")),
                Service.slug == resolved_slug,
            )
        )
        service = result.scalar_one_or_none()
    else:
        owner_result = await session.execute(
            select(User).where(User.org_id == identity.org_id, User.handle == owner_part)
        )
        owner_user = owner_result.scalar_one_or_none()
        if owner_user is not None:
            result = await session.execute(
                select(Service).where(
                    Service.org_id == identity.org_id,
                    Service.tier == "personal",
                    Service.owner_user_id == owner_user.id,
                    Service.slug == resolved_slug,
                )
            )
            service = result.scalar_one_or_none()
    if service is None or service.status == "deleted":
        raise ApiProblem("NOT_FOUND", "Service not found.", 404)
    if not await has_access(session, _viewer_identity(identity), service):
        raise ApiProblem("NOT_FOUND", "Service not found.", 404)
    is_admin = identity.user.role == "admin"
    is_owner = service.owner_user_id == identity.user.id
    if not (is_admin or is_owner):
        raise ApiProblem("FORBIDDEN", "You cannot change this service.", 403)
    return service


@router.post("/services")
async def create_service(
    request: Request, identity: IdentityDep, body: CreateServiceBody
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            service, _connection = await register_service(
                db,
                tier="personal",
                owner=identity.user,
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
        return {"service": service_public(service, identity.user.handle)}


@router.post("/services/test")
async def test_service(
    request: Request, identity: IdentityDep, body: CreateServiceBody
) -> dict[str, Any]:
    if body.kind == "mcp":
        if body.transport is None:
            raise ApiProblem("VALIDATION_ERROR", "transport is required for mcp services", 422)
        return await _test_mcp(request, identity, body)
    return await _test_rest(request, identity, body)


@router.post("/services/{owner}/{slug}/tools")
async def create_service_tool(
    request: Request,
    identity: IdentityDep,
    owner: str,
    slug: str,
    body: DefineToolBody,
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        service = await _lookup_manageable(db, identity, owner, slug)
        if service.kind != "rest":
            raise ApiProblem("VALIDATION_ERROR", "Manual tools require a REST service.", 422)
        try:
            tool = await define_rest_tool(
                db,
                service=service,
                name=body.name,
                method=body.method,
                path=body.path,
                params=body.params,
                description=body.description,
                input_schema=body.input_schema,
            )
            await db.commit()
        except RestToolError as exc:
            raise ApiProblem("VALIDATION_ERROR", str(exc), 422) from None
        owner_handle = await _owner_handle_of(db, service)
        return {"tool": tool_public(tool), "service": service_public(service, owner_handle)}
