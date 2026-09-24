from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, cast

from itsdangerous import BadSignature, URLSafeTimedSerializer

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


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=SESSION_SALT)


def issue_session(
    secret: str, *, user_id: uuid.UUID, org_id: uuid.UUID, now: int | None = None
) -> str:
    iat = int(time.time()) if now is None else now
    payload: dict[str, Any] = {"u": str(user_id), "o": str(org_id), "iat": iat}
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
    try:
        user_id = uuid.UUID(raw_user)
        org_id = uuid.UUID(raw_org)
    except ValueError:
        return None
    current = int(time.time()) if now is None else now
    if current - raw_iat > max_age:
        return None
    return SessionPayload(user_id=user_id, org_id=org_id, iat=raw_iat)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def session_needs_refresh(payload: SessionPayload, *, now: int | None = None) -> bool:
    current = int(time.time()) if now is None else now
    return current - payload.iat > SESSION_REFRESH_AFTER_S
