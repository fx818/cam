"""PR R16: login lockout must count targeted failures in SQL and admit
concurrent attempts atomically, bounding password verification.

The old code sampled the latest 500 failures globally and filtered email/IP in
Python (evictable by unrelated traffic) and did check + record as separate
unlocked operations (racy).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import UserEnv, ensure_csrf, login
from cam.auth.lockout import admit_attempt, locked_until, record_attempt
from cam.auth.sessions import CSRF_HEADER
from cam.db.models import LoginAttempt


def _failure(
    *, email: str, ip: str | None, ts: datetime, user_id: uuid.UUID | None = None
) -> dict[str, object]:
    return {
        "id": uuid.uuid4(),
        "org_id": None,
        "user_id": user_id,
        "email": email,
        "ip": ip,
        "ts": ts,
        "success": False,
    }


async def test_targeted_counts_survive_unrelated_failure_flood(
    db_session: AsyncSession,
) -> None:
    victim = f"victim-{uuid.uuid4().hex[:8]}@example.com"
    now = datetime.now(UTC)
    rows = [_failure(email=victim, ip=None, ts=now - timedelta(minutes=14)) for _ in range(5)]
    # A naive "latest 500 failures globally" sample would be entirely these.
    rows += [_failure(email=f"noise-{index}@example.com", ip=None, ts=now) for index in range(600)]
    await db_session.execute(insert(LoginAttempt), rows)
    await db_session.commit()

    lock = await locked_until(db_session, email=victim, ip=None, now=now)
    assert lock is not None
    assert lock > now


async def test_failures_outside_window_do_not_lock(db_session: AsyncSession) -> None:
    victim = f"victim-{uuid.uuid4().hex[:8]}@example.com"
    now = datetime.now(UTC)
    rows = [_failure(email=victim, ip=None, ts=now - timedelta(minutes=16)) for _ in range(5)]
    await db_session.execute(insert(LoginAttempt), rows)
    await db_session.commit()

    assert await locked_until(db_session, email=victim, ip=None, now=now) is None


async def test_record_attempt_locks_on_fifth_failure(db_session: AsyncSession) -> None:
    victim = f"victim-{uuid.uuid4().hex[:8]}@example.com"
    for _ in range(4):
        await record_attempt(db_session, email=victim, ip=None, success=False)
    await db_session.commit()
    assert await locked_until(db_session, email=victim, ip=None) is None

    await record_attempt(db_session, email=victim, ip=None, success=False)
    await db_session.commit()
    assert await locked_until(db_session, email=victim, ip=None) is not None


async def test_admit_attempt_allows_then_blocks(db_session: AsyncSession) -> None:
    victim = f"victim-{uuid.uuid4().hex[:8]}@example.com"
    assert await admit_attempt(db_session, email=victim, ip="203.0.113.7") is None

    for _ in range(5):
        await record_attempt(db_session, email=victim, ip="203.0.113.7", success=False)
    await db_session.commit()

    lock = await admit_attempt(db_session, email=victim, ip="203.0.113.7")
    assert lock is not None


async def test_ip_cap_locks_across_emails(db_session: AsyncSession) -> None:
    ip = "203.0.113.9"
    now = datetime.now(UTC)
    rows = [
        _failure(email=f"spread-{index}@example.com", ip=ip, ts=now - timedelta(minutes=1))
        for index in range(20)
    ]
    await db_session.execute(insert(LoginAttempt), rows)
    await db_session.commit()

    lock = await locked_until(db_session, email="fresh@example.com", ip=ip, now=now)
    assert lock is not None


async def test_concurrent_attempts_admitted_exactly_to_the_cap(env_user: UserEnv) -> None:
    email = env_user.user.email
    csrf = await ensure_csrf(env_user.client)
    headers = {CSRF_HEADER: csrf}

    async def attempt() -> int:
        response = await env_user.client.post(
            "/api/auth/login",
            json={"email": email, "password": "definitely-wrong"},
            headers=headers,
        )
        return response.status_code

    codes = await asyncio.gather(*[attempt() for _ in range(8)])
    assert codes.count(401) == 5
    assert codes.count(429) == 3

    # The same email is now locked even with the correct password.
    blocked = await login(env_user.client, email, "any-password")
    assert blocked.status_code == 429


async def test_locked_account_recovers_after_window(
    db_session: AsyncSession, env_user: UserEnv
) -> None:
    email = env_user.user.email
    stale = datetime.now(UTC) - timedelta(minutes=30)
    rows = [_failure(email=email, ip=None, ts=stale, user_id=env_user.user.id) for _ in range(5)]
    await db_session.execute(insert(LoginAttempt), rows)
    await db_session.commit()

    remaining = (
        await db_session.execute(select(LoginAttempt).where(LoginAttempt.email == email))
    ).scalars()
    assert len(list(remaining)) == 5
    assert await locked_until(db_session, email=email, ip=None) is None
