from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cam.db.models import LoginAttempt

WINDOW = timedelta(minutes=15)
LOCKOUT = timedelta(minutes=15)
MAX_FAILURES_PER_EMAIL = 5
MAX_FAILURES_PER_IP = 20


def _advisory_key(scope: str, value: str) -> int:
    """Stable signed 64-bit key for a Postgres advisory lock."""
    digest = hashlib.blake2b(f"{scope}:{value}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def _acquire_admission_lock(session: AsyncSession, *, scope: str, value: str) -> None:
    """Take a transaction-scoped advisory lock, serialising admissions.

    Held until the surrounding transaction commits or rolls back, so the
    count-then-record pair in the login route becomes atomic and the number of
    expensive (Argon2) verifications in flight for one identity is bounded.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": _advisory_key(scope, value)},
    )


async def _recent_failures(
    session: AsyncSession,
    *,
    cutoff: datetime,
    email: str | None = None,
    ip: str | None = None,
) -> tuple[int, datetime | None]:
    """Count and timestamp the most recent failure matching the predicate.

    Counted in SQL against the indexed columns, so unrelated failures can no
    longer evict the rows that matter (the old code sampled the latest 500
    failures globally and filtered in Python).
    """
    conditions = [LoginAttempt.success.is_(False), LoginAttempt.ts > cutoff]
    if email is not None:
        conditions.append(LoginAttempt.email == email)
    if ip is not None:
        conditions.append(LoginAttempt.ip == ip)
    result = await session.execute(
        select(func.count(), func.max(LoginAttempt.ts)).where(*conditions)
    )
    count, latest = result.one()
    return int(count or 0), latest


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
    email_count, email_latest = await _recent_failures(session, cutoff=cutoff, email=email)
    lock: datetime | None = None
    if email_count >= MAX_FAILURES_PER_EMAIL and email_latest is not None:
        lock = email_latest + LOCKOUT
    if ip is not None:
        ip_count, ip_latest = await _recent_failures(session, cutoff=cutoff, ip=ip)
        if ip_count >= MAX_FAILURES_PER_IP and ip_latest is not None:
            candidate = ip_latest + LOCKOUT
            if lock is None or candidate > lock:
                lock = candidate
    if lock is not None and lock > current:
        return lock
    return None


async def admit_attempt(
    session: AsyncSession, *, email: str, ip: str | None, now: datetime | None = None
) -> datetime | None:
    """Atomic admission gate to run *before* the password hash/comparison.

    Serialises concurrent sign-ins for the same email and IP with advisory
    locks, then returns the lock expiry if the caller is rate-limited, or
    ``None`` if the attempt may proceed. The caller must keep the same session
    (transaction) open until it records the outcome so the reservation is
    effective.
    """
    current = now if now is not None else datetime.now(UTC)
    await _acquire_admission_lock(session, scope="email", value=email)
    if ip is not None:
        await _acquire_admission_lock(session, scope="ip", value=ip)
    return await locked_until(session, email=email, ip=ip, now=current)
