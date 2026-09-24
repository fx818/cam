"""Fixtures for API route tests (dual auth: bearer tokens and cookie sessions)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import ApiEnv, UserEnv, api_client
from cam.access.shares import add_share
from cam.accounts.users import create_user
from cam.api.main import create_app
from cam.connections.orgs import ensure_org
from cam.connections.tokens import create_token
from cam.connections.url_policy import add_url_allowlist, remove_url_allowlist
from cam.db.models import Connection, LoginAttempt, Org, Service, Share, Tool, User
from cam.registry.services import register_global_mcp, register_personal_mcp


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


@pytest.fixture
async def admin_token(db_session: AsyncSession, api_admin: User) -> str:
    raw, _ = await create_token(
        db_session, org_id=api_admin.org_id, user_id=api_admin.id, label="admin-tests"
    )
    await db_session.commit()
    return raw


@pytest.fixture
async def second_password() -> str:
    return f"pw-{uuid.uuid4().hex}"


@pytest.fixture
async def second_user(db_session: AsyncSession, org: Org, second_password: str) -> User:
    suffix = uuid.uuid4().hex[:10]
    user = await create_user(
        db_session,
        email=f"other-{suffix}@example.com",
        password=second_password,
        role="user",
    )
    await db_session.commit()
    return user


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


@pytest.fixture
async def personal_service(db_session: AsyncSession, api_user: User) -> tuple[Service, Connection]:
    suffix = uuid.uuid4().hex[:8]
    service, connection = await register_personal_mcp(
        db_session,
        owner=api_user,
        slug=f"team-{suffix}",
        name="Team API",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="bearer",
        credential="orig-secret",
    )
    await db_session.commit()
    return service, connection


@pytest.fixture
async def global_service(db_session: AsyncSession) -> tuple[Service, Connection]:
    suffix = uuid.uuid4().hex[:8]
    service, connection = await register_global_mcp(
        db_session,
        slug=f"gs-{suffix}",
        name="Global Mock",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="static-header",
        header_name="X-Api-Key",
        credential="global-secret",
    )
    await db_session.commit()
    return service, connection


@pytest.fixture
async def template_service(db_session: AsyncSession, org: Org) -> Service:
    suffix = uuid.uuid4().hex[:8]
    service = Service(
        org_id=org.id,
        tier="template",
        owner_user_id=None,
        slug=f"tmpl-{suffix}",
        name="Notion",
        description="Template service",
        kind="mcp",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="oauth",
        auth_config={},
        visibility="all",
        status="active",
        health="unknown",
    )
    db_session.add(service)
    await db_session.flush()
    await db_session.commit()
    return service


@pytest.fixture
async def personal_tool(
    db_session: AsyncSession, personal_service: tuple[Service, Connection]
) -> Tool:
    service, _connection = personal_service
    tool = Tool(
        org_id=service.org_id,
        service_id=service.id,
        name="echo",
        upstream_name="echo",
        description="Echo the input",
        input_schema={"type": "object"},
        enabled=True,
        status="active",
    )
    db_session.add(tool)
    await db_session.flush()
    await db_session.commit()
    return tool


@pytest.fixture
async def active_share(
    db_session: AsyncSession,
    api_user: User,
    second_user: User,
    personal_service: tuple[Service, Connection],
) -> Share:
    _service, connection = personal_service
    share = await add_share(
        db_session,
        connection_id=connection.id,
        grantee_user_id=second_user.id,
        actor=api_user,
    )
    await db_session.commit()
    return share
