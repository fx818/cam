"""Service connect routes: start OAuth or store a secret connection.

`POST /api/services/{ref}/connect` accepts the dashboard's two-segment ref
(`owner/slug` or `global/slug`) via the `{owner}/{slug}` route, and a single
segment (UUID id) via `{ref}`. OAuth returns `{authorize_url}`; secret types
return `{connection}` after upserting the encrypted credential.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.access.resolution import GLOBAL_OWNER, has_access
from cam.api.deps import SessionIdentity, require_user
from cam.api.errors import ApiProblem
from cam.connections.crypto import MasterKeyRing, encrypt_secret, get_default_key_ring
from cam.connections.oauth import OAuthFlowError, complete_authorize, start_authorize
from cam.connections.orgs import get_org_data_key
from cam.connections.tokens import AuthenticatedIdentity
from cam.db.models import CamToken, Connection, OAuthState, Service, User
from cam.registry.resync import ResyncOutcome, resync_service_id

router = APIRouter(prefix="/api", tags=["connect"])

_FALLBACK_TASKS: set[asyncio.Task[ResyncOutcome]] = set()

IdentityDep = Annotated[SessionIdentity, Depends(require_user)]


class ConnectBody(BaseModel):
    credential: str | None = Field(default=None, max_length=8192)


def connection_public(connection: Connection) -> dict[str, Any]:
    return {
        "id": str(connection.id),
        "status": connection.status,
        "expires_at": connection.expires_at.isoformat() if connection.expires_at else None,
        "created_at": connection.created_at.isoformat() if connection.created_at else None,
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


async def _lookup_connectable(
    session: AsyncSession, identity: SessionIdentity, ref: str
) -> Service:
    service: Service | None = None
    if "/" not in ref:
        try:
            service_uuid = UUID(ref)
        except ValueError:
            raise ApiProblem("NOT_FOUND", "Service not found.", 404) from None
        service = await session.get(Service, service_uuid)
        if service is None or service.org_id != identity.org_id:
            raise ApiProblem("NOT_FOUND", "Service not found.", 404)
    else:
        owner_part, _, slug = ref.partition("/")
        owner_part = owner_part.strip().lower()
        resolved_slug = slug.strip()
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
    return service


async def _connection_user_id(service: Service, identity: SessionIdentity) -> UUID | None:
    if service.tier == "global":
        if identity.user.role != "admin":
            raise ApiProblem("FORBIDDEN", "Only admins can connect global services.", 403)
        return None
    if service.tier == "personal":
        if service.owner_user_id != identity.user.id and identity.user.role != "admin":
            raise ApiProblem("FORBIDDEN", "You cannot change this service.", 403)
        return service.owner_user_id
    return identity.user.id


async def _upsert_secret_connection(
    session: AsyncSession,
    *,
    service: Service,
    user_id: UUID | None,
    credential: str,
    ring: MasterKeyRing | None = None,
) -> Connection:
    key_ring = ring if ring is not None else get_default_key_ring()
    result = await session.execute(
        select(Connection).where(
            Connection.org_id == service.org_id,
            Connection.service_id == service.id,
            Connection.user_id == user_id,
        )
    )
    connection = result.scalar_one_or_none()
    key_version, data_key = await get_org_data_key(session, service.org_id, ring=key_ring)
    blob = encrypt_secret(credential.encode("utf-8"), data_key, key_version=key_version)
    if connection is None:
        connection = Connection(
            org_id=service.org_id,
            service_id=service.id,
            user_id=user_id,
            secret_enc=blob,
            key_version=key_version,
            status="connected",
        )
        session.add(connection)
    else:
        connection.secret_enc = blob
        connection.key_version = key_version
        connection.status = "connected"
        connection.expires_at = None
        connection.last_rotated_at = datetime.now(tz=UTC)
    if service.health == "needs_reconnect":
        service.health = "healthy"
    await session.flush()
    return connection


async def _ensure_no_auth_connection(
    session: AsyncSession, *, service: Service, user_id: UUID | None
) -> tuple[Connection, bool]:
    result = await session.execute(
        select(Connection).where(
            Connection.org_id == service.org_id,
            Connection.service_id == service.id,
            Connection.user_id == user_id,
        )
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        connection = Connection(
            org_id=service.org_id,
            service_id=service.id,
            user_id=user_id,
            status="connected",
        )
        session.add(connection)
        await session.flush()
        return connection, True
    return connection, False


def _enqueue_import(request: Request, service: Service) -> None:
    """Ask the app's resync coordinator to import tools now that a connection
    became usable (templates have no org credential, so this is how their
    first import happens). Fire-and-forget: the coordinator debounces and the
    per-service resync lock makes repeated enqueues cheap; the fallback task
    uses the same lock via `skip_if_running`."""
    coordinator = getattr(request.app.state, "resync_coordinator", None)
    if coordinator is not None:
        coordinator.enqueue(service.id)
        return
    session_factory = request.app.state.session_factory
    task = asyncio.create_task(resync_service_id(session_factory, service.id, skip_if_running=True))
    _FALLBACK_TASKS.add(task)
    task.add_done_callback(_FALLBACK_TASKS.discard)


async def _connect_service(
    request: Request, identity: SessionIdentity, service: Service, body: ConnectBody
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    settings = request.app.state.settings
    user_id = await _connection_user_id(service, identity)
    async with session_factory() as db:
        if service.auth_type == "oauth":
            try:
                authorize_url = await start_authorize(
                    db,
                    service=service,
                    user_id=identity.user.id,
                    base_url=settings.base_url,
                )
            except OAuthFlowError as exc:
                raise ApiProblem("BAD_REQUEST", str(exc), 400) from None
            await db.commit()
            return {"authorize_url": authorize_url}
        if service.auth_type == "none":
            connection, created = await _ensure_no_auth_connection(
                db, service=service, user_id=user_id
            )
            await db.commit()
            if created:
                _enqueue_import(request, service)
            return {"connection": connection_public(connection)}
        if body.credential is None or not body.credential.strip():
            raise ApiProblem("VALIDATION_ERROR", "credential is required for this auth type", 422)
        connection = await _upsert_secret_connection(
            db, service=service, user_id=user_id, credential=body.credential.strip()
        )
        await db.commit()
        _enqueue_import(request, service)
        return {"connection": connection_public(connection)}


@router.post("/services/{ref}/connect")
async def connect_service(
    request: Request, identity: IdentityDep, ref: str, body: ConnectBody | None = None
) -> dict[str, Any]:
    connect_body = body if body is not None else ConnectBody()
    async with request.app.state.session_factory() as db:
        service = await _lookup_connectable(db, identity, ref)
    return await _connect_service(request, identity, service, connect_body)


@router.post("/services/{owner}/{slug}/connect")
async def connect_service_ref(
    request: Request,
    identity: IdentityDep,
    owner: str,
    slug: str,
    body: ConnectBody | None = None,
) -> dict[str, Any]:
    ref = f"{owner}/{slug}"
    connect_body = body if body is not None else ConnectBody()
    async with request.app.state.session_factory() as db:
        service = await _lookup_connectable(db, identity, ref)
    return await _connect_service(request, identity, service, connect_body)


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    settings = request.app.state.settings
    base = settings.base_url.rstrip("/")
    if error:
        message = error_description or error
        raise ApiProblem("BAD_REQUEST", f"OAuth authorize failed: {message}", 400)
    if not code or not state:
        raise ApiProblem("VALIDATION_ERROR", "OAuth callback requires code and state", 422)
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        result = await db.execute(select(OAuthState).where(OAuthState.state == state))
        state_row = result.scalar_one_or_none()
        if state_row is None:
            raise ApiProblem("NOT_FOUND", "Unknown OAuth state.", 404)
        service = await db.get(Service, state_row.service_id)
        if service is None:
            raise ApiProblem("NOT_FOUND", "Service not found.", 404)
        try:
            await complete_authorize(
                db,
                service=service,
                code=code,
                state=state,
                base_url=settings.base_url,
            )
        except OAuthFlowError as exc:
            raise ApiProblem("BAD_REQUEST", str(exc), 400) from None
        await db.commit()
        _enqueue_import(request, service)
    return RedirectResponse(url=f"{base}/", status_code=302)
