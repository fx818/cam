from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import ApiEnv, UserEnv
from cam.accounts.users import create_user
from cam.api.main import create_app
from cam.connections.orgs import ensure_org
from cam.connections.tokens import create_token
from cam.db.models import LoginAttempt, Org, User


@asynccontextmanager
async def _run_lifespan(app: FastAPI) -> AsyncGenerator[None]:
    stop = asyncio.Event()
    started = asyncio.Event()
    failure: list[BaseException] = []

    async def _lifespan() -> None:
        try:
            async with app.router.lifespan_context(app):
                started.set()
                await stop.wait()
        except BaseException as exc:
            failure.append(exc)
            started.set()
            raise

    task = asyncio.create_task(_lifespan(), name="cam-api-test-lifespan")
    await started.wait()
    if failure:
        await task
    try:
        yield
    finally:
        stop.set()
        await task


@asynccontextmanager
async def api_client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    async with _run_lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test", timeout=30.0
        ) as client:
            yield client


@pytest.fixture
async def api() -> AsyncGenerator[ApiEnv]:
    app = create_app()
    async with api_client(app) as client:
        yield ApiEnv(app, client)


@pytest.fixture
async def env_user(api: ApiEnv, api_user: User) -> UserEnv:
    return UserEnv(api.app, api.client, api_user)


@pytest.fixture
async def org(db_session: AsyncSession) -> Org:
    org_row = await ensure_org(db_session)
    await db_session.commit()
    return org_row


@pytest.fixture(autouse=True)
async def clean_login_attempts(db_session: AsyncSession) -> AsyncGenerator[None]:
    yield
    await db_session.execute(delete(LoginAttempt))
    await db_session.commit()


@pytest.fixture
async def user_password() -> str:
    return f"pw-{uuid.uuid4().hex}"


@pytest.fixture
async def api_user(db_session: AsyncSession, org: Org, user_password: str) -> User:
    suffix = uuid.uuid4().hex[:10]
    user = await create_user(
        db_session,
        email=f"user-{suffix}@example.com",
        password=user_password,
        role="user",
    )
    await db_session.commit()
    return user


@pytest.fixture
async def api_admin(db_session: AsyncSession, org: Org, user_password: str) -> User:
    suffix = uuid.uuid4().hex[:10]
    admin = await create_user(
        db_session,
        email=f"admin-{suffix}@example.com",
        password=user_password,
        role="admin",
    )
    await db_session.commit()
    return admin


@pytest.fixture
async def admin_env(api: ApiEnv, api_admin: User) -> UserEnv:
    return UserEnv(api.app, api.client, api_admin)


@pytest.fixture
async def user_token(db_session: AsyncSession, api_user: User) -> str:
    raw, _ = await create_token(
        db_session, org_id=api_user.org_id, user_id=api_user.id, label="test-agent"
    )
    await db_session.commit()
    return raw
