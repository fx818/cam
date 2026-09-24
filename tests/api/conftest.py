"""Fixtures for Phase 3 API route tests (bearer auth, personal services)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import ApiEnv, api_client
from cam.accounts.users import create_user
from cam.api.main import create_app
from cam.connections.orgs import ensure_org
from cam.connections.tokens import create_token
from cam.connections.url_policy import add_url_allowlist, remove_url_allowlist
from cam.db.models import Org, User


@pytest.fixture
async def api() -> AsyncGenerator[ApiEnv]:
    app = create_app()
    async with api_client(app) as client:
        yield ApiEnv(app, client)


@pytest.fixture
async def org(db_session: AsyncSession) -> Org:
    org_row = await ensure_org(db_session)
    await db_session.commit()
    return org_row


@pytest.fixture
async def api_user(db_session: AsyncSession, org: Org) -> User:
    suffix = uuid.uuid4().hex[:10]
    user = await create_user(
        db_session,
        email=f"api-{suffix}@example.com",
        password=f"pw-{uuid.uuid4().hex}",
        role="user",
    )
    await db_session.commit()
    return user


@pytest.fixture
async def api_admin(db_session: AsyncSession, org: Org) -> User:
    suffix = uuid.uuid4().hex[:10]
    admin = await create_user(
        db_session,
        email=f"admin-{suffix}@example.com",
        password=f"pw-{uuid.uuid4().hex}",
        role="admin",
    )
    await db_session.commit()
    return admin


@pytest.fixture
async def user_token(db_session: AsyncSession, api_user: User) -> str:
    raw, _ = await create_token(
        db_session, org_id=api_user.org_id, user_id=api_user.id, label="api-tests"
    )
    await db_session.commit()
    return raw


@pytest.fixture
async def admin_token(db_session: AsyncSession, api_admin: User) -> str:
    raw, _ = await create_token(
        db_session, org_id=api_admin.org_id, user_id=api_admin.id, label="admin-tests"
    )
    await db_session.commit()
    return raw


@pytest.fixture
async def loopback_allowlist(db_session: AsyncSession, org: Org) -> AsyncGenerator[None]:
    try:
        await add_url_allowlist(db_session, pattern="127.0.0.0/8", org_slug=org.slug)
        await db_session.commit()
    except Exception:
        await db_session.rollback()
    try:
        yield
    finally:
        try:
            await remove_url_allowlist(db_session, pattern="127.0.0.0/8", org_slug=org.slug)
            await db_session.commit()
        except Exception:
            await db_session.rollback()
