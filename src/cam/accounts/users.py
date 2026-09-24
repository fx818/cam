from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cam.connections.orgs import DEFAULT_ORG_SLUG, ensure_org
from cam.connections.passwords import hash_password
from cam.db.models import CamToken, Connection, User
from cam.upstream.pool import SessionPool

UserRole = Literal["admin", "user"]
UserKind = Literal["person", "team"]


class UserError(Exception):
    pass


class EmailAlreadyRegistered(UserError):
    pass


class HandleAlreadyTaken(UserError):
    pass


def normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if normalized.count("@") != 1:
        raise UserError("email must contain exactly one @")
    local_part, domain = normalized.split("@")
    if not local_part or not domain:
        raise UserError("email must have a local part and a domain")
    return normalized


def derive_handle(email: str) -> str:
    return normalize_email(email).split("@")[0]


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    normalized = normalize_email(email)
    result = await session.execute(select(User).where(User.email == normalized))
    return result.scalar_one_or_none()


async def deactivate_user(
    session: AsyncSession,
    *,
    email: str | None = None,
    user: User | None = None,
    pool: SessionPool | None = None,
) -> User:
    """Deactivate a user: revoke tokens, disable connections, close sessions.

    Idempotent. The user row is kept (audit history and handle stay reserved).
    """
    if (email is None) == (user is None):
        raise UserError("provide exactly one of email or user")
    if user is None:
        assert email is not None
        user = await get_user_by_email(session, email)
        if user is None:
            raise UserError(f"no user with email {email}")
    if user.status == "deactivated" and user.deactivated_at is not None:
        return user

    now = datetime.now(UTC)
    user.status = "deactivated"
    user.deactivated_at = now
    await session.execute(
        update(CamToken)
        .where(CamToken.user_id == user.id, CamToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    connections = list(
        (
            await session.execute(
                select(Connection).where(
                    Connection.org_id == user.org_id, Connection.user_id == user.id
                )
            )
        ).scalars()
    )
    for connection in connections:
        connection.status = "disabled"
    await session.flush()
    if pool is not None:
        for connection in connections:
            await pool.invalidate_connection(str(connection.service_id), str(connection.id))
    return user


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    role: UserRole,
    kind: UserKind = "person",
    handle: str | None = None,
    display_name: str | None = None,
    org_slug: str = DEFAULT_ORG_SLUG,
) -> User:
    if not password:
        raise UserError("password must not be empty")
    normalized = normalize_email(email)
    raw_local_part = email.strip().split("@")[0]
    resolved_handle = handle.strip().lower() if handle else derive_handle(normalized)
    if not resolved_handle:
        raise UserError("handle must not be empty")
    if "/" in resolved_handle:
        raise UserError("handle must not contain /")
    resolved_display = display_name if display_name is not None else raw_local_part
    if not resolved_display.strip():
        raise UserError("display name must not be empty")

    org = await ensure_org(session, slug=org_slug)
    existing_email = await session.execute(select(User.id).where(User.email == normalized))
    if existing_email.first() is not None:
        raise EmailAlreadyRegistered(f"email {normalized} is already registered")
    existing_handle = await session.execute(
        select(User.id).where(User.org_id == org.id, User.handle == resolved_handle)
    )
    if existing_handle.first() is not None:
        raise HandleAlreadyTaken(f"handle {resolved_handle!r} is already taken in this org")

    password_hash = await hash_password(password)
    user = User(
        org_id=org.id,
        email=normalized,
        handle=resolved_handle,
        display_name=resolved_display,
        password_hash=password_hash,
        role=role,
        kind=kind,
        status="active",
    )
    session.add(user)
    await session.flush()
    return user
