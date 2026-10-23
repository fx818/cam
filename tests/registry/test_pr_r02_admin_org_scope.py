"""R02 — administrative operations must stay inside the admin's own org.

The bug: `create_user`, `register_service` (global/template) and the visibility
helpers resolved the *default* org via `ensure_org(session)`, so an admin of a
second org could create users/services in IndiaMART's org. `list_admin_services`
already filtered by `admin.org_id`; these helpers now take an explicit `org_id`.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import api_client, login, post_json
from cam.accounts.users import UserError, create_user, get_user_by_email
from cam.api.main import create_app
from cam.connections.orgs import ensure_org
from cam.db.models import Org, Service
from cam.registry.services import (
    ServiceError,
    get_admin_service,
    register_global_mcp,
    register_service,
    set_service_visibility,
)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _other_org(db_session: AsyncSession) -> Org:
    return await ensure_org(db_session, slug=f"orgb-{uuid.uuid4().hex[:10]}", name="Org B")


async def _make_admin(db_session: AsyncSession, org: Org, password: str = "long-enough-pw"):
    return await create_user(
        db_session,
        org_id=org.id,
        email=f"adminb-{uuid.uuid4().hex[:10]}@example.com",
        password=password,
        role="admin",
    )


async def test_register_template_uses_explicit_admin_org(db_session: AsyncSession) -> None:
    org_b = await _other_org(db_session)
    admin_b = await _make_admin(db_session, org_b)
    slug = _unique("tplb")
    service, _conn = await register_service(
        db_session,
        tier="template",
        slug=slug,
        name="Org B template",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="none",
        org_id=admin_b.org_id,
    )
    assert service.org_id == org_b.id
    rows = (await db_session.execute(select(Service).where(Service.slug == slug))).scalars().all()
    # It must not have leaked into the default (IndiaMART) org.
    assert {row.org_id for row in rows} == {org_b.id}


async def test_create_user_uses_explicit_org(db_session: AsyncSession) -> None:
    org_b = await _other_org(db_session)
    user = await create_user(
        db_session,
        org_id=org_b.id,
        email=f"member-{uuid.uuid4().hex[:10]}@example.com",
        password="pw-whatever",
        role="user",
    )
    assert user.org_id == org_b.id


async def test_create_user_rejects_unknown_org(db_session: AsyncSession) -> None:
    with pytest.raises(UserError, match="does not exist"):
        await create_user(
            db_session,
            org_id=uuid.uuid4(),
            email=f"ghostorg-{uuid.uuid4().hex[:10]}@example.com",
            password="pw-whatever",
            role="user",
        )


async def test_visibility_helpers_are_org_scoped(db_session: AsyncSession) -> None:
    default_org = await ensure_org(db_session)
    slug = _unique("glob")
    await register_global_mcp(
        db_session,
        slug=slug,
        name="Global",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="none",
    )
    org_b = await _other_org(db_session)

    # Org B's admin cannot see or mutate the default org's service.
    with pytest.raises(ServiceError, match="no global or template"):
        await get_admin_service(db_session, slug=slug, org_id=org_b.id)
    with pytest.raises(ServiceError, match="no global or template"):
        await set_service_visibility(
            db_session, slug=slug, visibility="restricted", org_id=org_b.id
        )

    # A service registered in org B is reachable with org B's id, not the default.
    b_slug = _unique("bvis")
    await register_service(
        db_session,
        tier="template",
        slug=b_slug,
        name="Org B",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="none",
        org_id=org_b.id,
    )
    assert (await get_admin_service(db_session, slug=b_slug, org_id=org_b.id)).slug == b_slug
    with pytest.raises(ServiceError, match="no global or template"):
        await get_admin_service(db_session, slug=b_slug, org_id=default_org.id)


async def test_admin_http_route_scopes_creates_to_admin_org(db_session: AsyncSession) -> None:
    password = "long-enough-pw"
    app = create_app()
    async with api_client(app) as client:
        default_org = await ensure_org(db_session)
        org_b = await _other_org(db_session)
        admin_b = await _make_admin(db_session, org_b, password)
        await db_session.commit()

        login_response = await login(client, admin_b.email, password)
        assert login_response.status_code == 200

        member_email = f"member-{uuid.uuid4().hex[:10]}@example.com"
        created_user = await post_json(
            client,
            "/api/admin/users",
            {"email": member_email, "password": "long-enough-pw", "role": "user"},
        )
        assert created_user.status_code == 201
        stored = await get_user_by_email(db_session, member_email)
        assert stored is not None
        assert stored.org_id == org_b.id
        assert stored.org_id != default_org.id

        slug = _unique("httptpl")
        created_service = await post_json(
            client,
            "/api/admin/services",
            {
                "slug": slug,
                "name": "Org B route template",
                "url": "https://example.com/mcp",
                "transport": "streamable-http",
                "kind": "mcp",
                "tier": "template",
                "auth_type": "none",
                "test": False,
            },
        )
        assert created_service.status_code == 200, created_service.text
        rows = (
            (await db_session.execute(select(Service).where(Service.slug == slug))).scalars().all()
        )
        assert {row.org_id for row in rows} == {org_b.id}
