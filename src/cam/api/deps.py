"""Request identity for the JSON API.

Dual authentication: a `cam_` bearer token is checked first (agents and
scripted callers), then the dashboard session cookie. Session helpers come
from `cam.auth.sessions`; the cookie helpers below are for the auth routes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Request, Response

from cam.api.errors import ApiProblem
from cam.auth.sessions import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SESSION_IDLE_TIMEOUT_S,
    issue_session,
    load_session,
    session_needs_refresh,
)
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


def set_auth_cookies(
    response: Response, request: Request, session_value: str, csrf_token: str
) -> None:
    secure = request.url.scheme == "https"
    response.set_cookie(
        SESSION_COOKIE,
        session_value,
        max_age=SESSION_IDLE_TIMEOUT_S,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=SESSION_IDLE_TIMEOUT_S,
        httponly=False,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookies(response: Response, request: Request) -> None:
    secure = request.url.scheme == "https"
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="lax", secure=secure)
    response.delete_cookie(CSRF_COOKIE, path="/", samesite="lax", secure=secure)


async def load_identity(request: Request, response: Response) -> SessionIdentity | None:
    header = request.headers.get("authorization") or ""
    if header:
        return await _bearer_identity(request)
    settings = request.app.state.settings
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    payload = load_session(settings.session_secret, raw, max_age=SESSION_IDLE_TIMEOUT_S)
    if payload is None:
        return None
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        user = await db.get(User, payload.user_id)
        if user is None or user.status != "active" or user.deactivated_at is not None:
            return None
        if user.org_id != payload.org_id:
            return None
        # Revocation: a session signed before the last version bump (logout,
        # self-service or admin password change) no longer matches.
        if payload.version != (user.session_version or 0):
            return None
        org = await db.get(Org, user.org_id)
        if org is None:
            return None
        if session_needs_refresh(payload):
            fresh = issue_session(
                settings.session_secret,
                user_id=user.id,
                org_id=user.org_id,
                version=user.session_version or 0,
            )
            response.set_cookie(
                SESSION_COOKIE,
                fresh,
                max_age=SESSION_IDLE_TIMEOUT_S,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
                path="/",
            )
        return SessionIdentity(user=user, org=org, org_id=user.org_id)


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


def user_public(user: User) -> dict[str, str]:
    return {
        "id": str(user.id),
        "email": user.email,
        "handle": user.handle,
        "display_name": user.display_name,
        "role": user.role,
        "kind": user.kind,
        "status": user.status,
    }


def org_public(org: Org) -> dict[str, object]:
    settings = org.settings or {}
    branding = cast("dict[str, object]", settings.get("brand") or {})
    return {
        "id": str(org.id),
        "slug": org.slug,
        "name": org.name,
        "settings": {
            "brand": branding,
            "instructions": settings.get("server_instructions") or settings.get("instructions", ""),
            "allowed_email_domains": settings.get("allowed_email_domains", []),
        },
    }


def me_payload(identity: SessionIdentity, base_url: str) -> dict[str, object]:
    return {
        "user": user_public(identity.user),
        "org": org_public(identity.org),
        "base_url": base_url,
    }
