from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, cast

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from cam.db.models import User

SESSION_COOKIE = "cam_session"
CSRF_COOKIE = "cam_csrf"
CSRF_HEADER = "X-CSRF-Token"
SESSION_SALT = "cam.dashboard.session"
SESSION_IDLE_TIMEOUT_S = 12 * 60 * 60
SESSION_REFRESH_AFTER_S = 300


@dataclass(frozen=True, slots=True)
class SessionPayload:
    user_id: uuid.UUID
    org_id: uuid.UUID
    iat: int
    # `user.session_version` at issue time. A mismatch means the session was
    # revoked (logout, self-service or admin password change) and must be
    # rejected even though the signature is still valid.
    version: int = 0


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=SESSION_SALT)


def issue_session(
    secret: str,
    *,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    version: int = 0,
    now: int | None = None,
) -> str:
    iat = int(time.time()) if now is None else now
    payload: dict[str, Any] = {
        "u": str(user_id),
        "o": str(org_id),
        "iat": iat,
        "v": int(version),
    }
    return _serializer(secret).dumps(payload)


def load_session(
    secret: str, raw: str, *, max_age: int = SESSION_IDLE_TIMEOUT_S, now: int | None = None
) -> SessionPayload | None:
    try:
        loaded: object = _serializer(secret).loads(raw, max_age=max_age)
    except BadSignature:
        return None
    if not isinstance(loaded, dict):
        return None
    data = cast("dict[str, object]", loaded)
    raw_user = data.get("u")
    raw_org = data.get("o")
    raw_iat = data.get("iat")
    if not isinstance(raw_user, str) or not isinstance(raw_org, str):
        return None
    if not isinstance(raw_iat, int):
        return None
    raw_version = data.get("v", 0)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raw_version = 0
    try:
        user_id = uuid.UUID(raw_user)
        org_id = uuid.UUID(raw_org)
    except ValueError:
        return None
    current = int(time.time()) if now is None else now
    if current - raw_iat > max_age:
        return None
    return SessionPayload(user_id=user_id, org_id=org_id, iat=raw_iat, version=raw_version)


async def invalidate_user_sessions(session: AsyncSession, user: User) -> int:
    """Revoke every session issued for `user` by bumping `session_version`.

    Any signed cookie whose embedded version no longer matches is rejected by
    `cam.api.deps.load_identity`. Callers must commit. Returns the new version
    so a caller (e.g. a self-service password change) can re-issue a fresh
    cookie for the current device.
    """
    new_version = (user.session_version or 0) + 1
    user.session_version = new_version
    await session.flush()
    return new_version


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def session_needs_refresh(payload: SessionPayload, *, now: int | None = None) -> bool:
    current = int(time.time()) if now is None else now
    return current - payload.iat > SESSION_REFRESH_AFTER_S
