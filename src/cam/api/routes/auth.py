from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from cam.api.deps import (
    SessionIdentity,
    clear_session_cookies,
    client_ip,
    load_identity,
    me_payload,
    set_auth_cookies,
)
from cam.api.errors import ApiProblem
from cam.auth.lockout import admit_attempt, record_attempt
from cam.auth.sessions import (
    invalidate_user_sessions,
    issue_session,
    new_csrf_token,
)
from cam.connections.orgs import ensure_org
from cam.db.models import Org, User

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict[str, object]:
    email = body.email.strip().lower()
    ip = client_ip(request)
    session_factory = request.app.state.session_factory
    settings = request.app.state.settings
    provider = request.app.state.auth_provider
    async with session_factory() as db:
        await ensure_org(db)
        await db.commit()
    async with session_factory() as db:
        # Atomic admission gate: SQL-side targeted counts under an advisory
        # lock, taken before the expensive password verification.
        lock = await admit_attempt(db, email=email, ip=ip)
        if lock is not None:
            raise ApiProblem(
                "RATE_LIMITED",
                "Too many failed sign-in attempts. Try again in a few minutes.",
                429,
            )
        user = await provider.authenticate(db, email, body.password)
        if user is None:
            await record_attempt(db, email=email, ip=ip, success=False)
            await db.commit()
            raise ApiProblem("UNAUTHORIZED", "Invalid email or password.", 401)
        await record_attempt(
            db,
            email=email,
            ip=ip,
            success=True,
            user_id=user.id,
            org_id=user.org_id,
        )
        org = await db.get(Org, user.org_id)
        if org is None:
            raise ApiProblem("INTERNAL", "Org not found.", 500)
        session_value = issue_session(
            settings.session_secret,
            user_id=user.id,
            org_id=user.org_id,
            version=user.session_version or 0,
        )
        csrf_token = new_csrf_token()
        await db.commit()
    set_auth_cookies(response, request, session_value, csrf_token)
    return me_payload(SessionIdentity(user=user, org=org, org_id=user.org_id), settings.base_url)


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    # Revoke the session server-side (not just clearing the cookie) so a stolen
    # cookie is useless after logout.
    identity = await load_identity(request, response)
    if identity is not None:
        session_factory = request.app.state.session_factory
        async with session_factory() as db:
            user = await db.get(User, identity.user.id)
            if user is not None:
                await invalidate_user_sessions(db, user)
                await db.commit()
    clear_session_cookies(response, request)
    return {"ok": True}


@router.get("/me")
async def me(request: Request, response: Response) -> dict[str, object]:
    identity = await load_identity(request, response)
    if identity is None:
        raise ApiProblem("UNAUTHORIZED", "Sign in to continue.", 401)
    settings = request.app.state.settings
    return me_payload(identity, settings.base_url)
