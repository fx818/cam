"""Request identity for the JSON API.

Dual authentication: a `cam_` bearer token is checked first (works today and
keeps agent-style tests running), then the dashboard session cookie branch
when `cam.auth.sessions` is present (lands with the Phase 4 merge). The
session path is imported lazily so this module works before that merge.
"""

from __future__ import annotations

import importlib
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request, Response

from cam.api.errors import ApiProblem
from cam.connections.tokens import TOKEN_PREFIX, authenticate_token
from cam.db.models import Org, User


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    user: User
    org: Org
    org_id: uuid.UUID


def client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


async def _bearer_identity(request: Request) -> SessionIdentity | None:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    raw = header[7:].strip()
    if not raw.startswith(TOKEN_PREFIX):
        return None
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        authed = await authenticate_token(db, raw)
        if authed is None:
            return None
        org = await db.get(Org, authed.org_id)
        if org is None:
            return None
        return SessionIdentity(user=authed.user, org=org, org_id=authed.org_id)


async def _session_cookie_identity(request: Request, response: Response) -> SessionIdentity | None:
    try:
        sessions = importlib.import_module("cam.auth.sessions")
    except ImportError:
        return None
    session_cookie: str = sessions.SESSION_COOKIE
    idle_timeout: int = sessions.SESSION_IDLE_TIMEOUT_S
    issue_session = sessions.issue_session
    load_session = sessions.load_session
    session_needs_refresh = sessions.session_needs_refresh
    settings = request.app.state.settings
    raw = request.cookies.get(session_cookie)
    if not raw:
        return None
    payload: Any = load_session(settings.session_secret, raw, max_age=idle_timeout)
    if payload is None:
        return None
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        user = await db.get(User, payload.user_id)
        if user is None or user.status != "active" or user.deactivated_at is not None:
            return None
        if user.org_id != payload.org_id:
            return None
        org = await db.get(Org, user.org_id)
        if org is None:
            return None
        if session_needs_refresh(payload):
            fresh = issue_session(settings.session_secret, user_id=user.id, org_id=user.org_id)
            response.set_cookie(
                session_cookie,
                fresh,
                max_age=idle_timeout,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
                path="/",
            )
        return SessionIdentity(user=user, org=org, org_id=user.org_id)


async def load_identity(request: Request, response: Response) -> SessionIdentity | None:
    header = request.headers.get("authorization") or ""
    if header:
        return await _bearer_identity(request)
    return await _session_cookie_identity(request, response)


async def require_user(request: Request, response: Response) -> SessionIdentity:
    identity = await load_identity(request, response)
    if identity is None:
        raise ApiProblem("UNAUTHORIZED", "Sign in to continue.", 401)
    return identity


async def require_admin(
    identity: Annotated[SessionIdentity, Depends(require_user)],
) -> SessionIdentity:
    if identity.user.role != "admin":
        raise ApiProblem("FORBIDDEN", "Admin access required.", 403)
    return identity
