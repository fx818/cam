from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.db.models import LoginAttempt

WINDOW = timedelta(minutes=15)
LOCKOUT = timedelta(minutes=15)
MAX_FAILURES_PER_EMAIL = 5
MAX_FAILURES_PER_IP = 20
_ROW_LIMIT = 500


async def record_attempt(
    session: AsyncSession,
    *,
    email: str,
    ip: str | None,
    success: bool,
    user_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
) -> None:
    session.add(
        LoginAttempt(
            org_id=org_id,
            user_id=user_id,
            email=email,
            ip=ip,
            ts=datetime.now(UTC),
            success=success,
        )
    )
    await session.flush()


async def locked_until(
    session: AsyncSession, *, email: str, ip: str | None, now: datetime | None = None
) -> datetime | None:
    current = now if now is not None else datetime.now(UTC)
    cutoff = current - WINDOW
    result = await session.execute(
        select(LoginAttempt.email, LoginAttempt.ip, LoginAttempt.ts)
        .where(LoginAttempt.ts > cutoff, LoginAttempt.success.is_(False))
        .order_by(LoginAttempt.ts.desc())
        .limit(_ROW_LIMIT)
    )
    rows = result.all()
    email_ts = [ts for (row_email, _row_ip, ts) in rows if row_email == email]
    ip_ts = [] if ip is None else [ts for (_row_email, row_ip, ts) in rows if row_ip == ip]
    lock: datetime | None = None
    if len(email_ts) >= MAX_FAILURES_PER_EMAIL:
        lock = max(email_ts) + LOCKOUT
    if len(ip_ts) >= MAX_FAILURES_PER_IP:
        candidate = max(ip_ts) + LOCKOUT
        if lock is None or candidate > lock:
            lock = candidate
    if lock is not None and lock > current:
        return lock
    return None
