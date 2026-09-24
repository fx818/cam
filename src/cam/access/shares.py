"""Sharing: one connection shared with specific users, all-or-nothing.

Shares live on the owner's connection. Only the connection owner can add
or revoke them, so access is never re-shareable. Revocation takes effect
on the next call; nothing caches access decisions across calls.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.access.resolution import parse_service_ref
from cam.accounts.users import get_user_by_email
from cam.db.models import Connection, Service, Share, User


class ShareError(Exception):
    pass


async def _owned_connection(
    session: AsyncSession, *, connection_id: uuid.UUID, actor: User
) -> Connection:
    connection = await session.get(Connection, connection_id)
    if connection is None or connection.org_id != actor.org_id or connection.user_id != actor.id:
        raise ShareError("connection not found")
    return connection


async def _require_active_user(session: AsyncSession, *, email: str, field: str) -> User:
    user = await get_user_by_email(session, email)
    if user is None:
        raise ShareError(f"no user with email {email}")
    if user.status != "active" or user.deactivated_at is not None:
        raise ShareError(f"{field} is not active")
    return user


async def active_share(
    session: AsyncSession, *, connection_id: uuid.UUID, grantee_user_id: uuid.UUID
) -> Share | None:
    result = await session.execute(
        select(Share).where(
            Share.connection_id == connection_id,
            Share.grantee_user_id == grantee_user_id,
            Share.revoked_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def add_share(
    session: AsyncSession, *, connection_id: uuid.UUID, grantee_user_id: uuid.UUID, actor: User
) -> Share:
    connection = await _owned_connection(session, connection_id=connection_id, actor=actor)
    grantee = await session.get(User, grantee_user_id)
    if grantee is None or grantee.org_id != actor.org_id:
        raise ShareError("grantee user not found in this org")
    if grantee.id == actor.id:
        raise ShareError("cannot share with yourself")
    if grantee.status != "active" or grantee.deactivated_at is not None:
        raise ShareError(f"user {grantee.email} is not active")
    existing = await active_share(session, connection_id=connection.id, grantee_user_id=grantee.id)
    if existing is not None:
        raise ShareError(f"service is already shared with {grantee.email}")
    share = Share(
        org_id=connection.org_id,
        connection_id=connection.id,
        grantee_user_id=grantee.id,
    )
    session.add(share)
    await session.flush()
    return share


async def revoke_share(session: AsyncSession, *, share_id: uuid.UUID, actor: User) -> Share:
    share = await session.get(Share, share_id)
    if share is None:
        raise ShareError("share not found")
    await _owned_connection(session, connection_id=share.connection_id, actor=actor)
    if share.revoked_at is not None:
        raise ShareError("share is already revoked")
    share.revoked_at = datetime.now(UTC)
    await session.flush()
    return share


async def list_shares(
    session: AsyncSession, *, connection_id: uuid.UUID, actor: User
) -> list[Share]:
    connection = await _owned_connection(session, connection_id=connection_id, actor=actor)
    result = await session.execute(
        select(Share)
        .where(Share.connection_id == connection.id)
        .order_by(Share.created_at, Share.id)
    )
    return list(result.scalars())


async def _resolve_owned_service(
    session: AsyncSession, *, ref: str, owner_email: str
) -> tuple[User, Service, Connection]:
    owner = await _require_active_user(session, email=owner_email, field="owner")
    owner_part, slug = parse_service_ref(ref)
    if owner_part == "global":
        raise ShareError("global services are shared through visibility, not connection shares")
    if owner_part != owner.handle:
        raise ShareError(f"service {ref!r} does not belong to {owner.email}")
    service_result = await session.execute(
        select(Service).where(
            Service.org_id == owner.org_id,
            Service.tier == "personal",
            Service.owner_user_id == owner.id,
            Service.slug == slug,
        )
    )
    service = service_result.scalar_one_or_none()
    if service is None:
        raise ShareError(f"no personal service with id {ref!r}")
    connection_result = await session.execute(
        select(Connection).where(
            Connection.org_id == owner.org_id,
            Connection.service_id == service.id,
            Connection.user_id == owner.id,
        )
    )
    connection = connection_result.scalar_one_or_none()
    if connection is None:
        raise ShareError(f"service {ref!r} has no connection to share")
    return owner, service, connection


async def share_service(
    session: AsyncSession, *, ref: str, owner_email: str, grantee_email: str
) -> Share:
    owner, _service, connection = await _resolve_owned_service(
        session, ref=ref, owner_email=owner_email
    )
    grantee = await _require_active_user(session, email=grantee_email, field="grantee")
    if grantee.id == owner.id:
        raise ShareError("cannot share with yourself")
    return await add_share(
        session, connection_id=connection.id, grantee_user_id=grantee.id, actor=owner
    )


async def unshare_service(
    session: AsyncSession, *, ref: str, owner_email: str, grantee_email: str
) -> Share:
    owner, _service, connection = await _resolve_owned_service(
        session, ref=ref, owner_email=owner_email
    )
    grantee = await get_user_by_email(session, grantee_email)
    if grantee is None:
        raise ShareError(f"no user with email {grantee_email}")
    share = await active_share(session, connection_id=connection.id, grantee_user_id=grantee.id)
    if share is None:
        raise ShareError(f"service is not shared with {grantee.email}")
    return await revoke_share(session, share_id=share.id, actor=owner)
