"""R23: a typed effective-settings resolver, and aligned setting key names.

The dashboard writes `server_instructions` (the key the gateway and the new
resolver read); the endpoint also surfaces the resolved "effective" values.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import UserEnv, login, mutate_json
from cam.api.settings_resolver import OrgEffective, effective_settings, org_effective_from_settings
from cam.connections.orgs import DEFAULT_ORG_SLUG
from cam.db.models import Org
from cam.gateway.server import load_server_instructions


@pytest.fixture(autouse=True)
async def clean_org_settings(db_session: AsyncSession) -> AsyncGenerator[None]:
    await db_session.execute(update(Org).where(Org.slug == DEFAULT_ORG_SLUG).values(settings={}))
    await db_session.commit()
    yield
    await db_session.execute(update(Org).where(Org.slug == DEFAULT_ORG_SLUG).values(settings={}))
    await db_session.commit()


def test_resolver_unset_fields_are_none() -> None:
    empty = org_effective_from_settings(None)
    assert empty == OrgEffective()
    assert empty.server_instructions is None
    assert empty.default_timeout_s is None
    assert empty.default_response_cap_tokens is None
    assert empty.log_retention_days is None
    assert empty.credential_stale_days is None


async def test_resolver_reads_canonical_settings(db_session: AsyncSession, org: Org) -> None:
    org.settings = {
        "server_instructions": "hello",
        "default_timeout_s": 45,
        "default_response_cap_tokens": 1234,
        "log_retention_days": 7,
        "credential_stale_days": 12,
    }
    await db_session.commit()

    effective = await effective_settings(db_session, org.id)
    assert effective.server_instructions == "hello"
    assert effective.default_timeout_s == 45
    assert effective.default_response_cap_tokens == 1234
    assert effective.log_retention_days == 7
    assert effective.credential_stale_days == 12


async def test_admin_settings_writes_canonical_key_and_surfaces_effective(
    admin_env: UserEnv,
    user_password: str,
    db_session: AsyncSession,
    org: Org,
) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)

    response = await mutate_json(
        admin_env.client,
        "PATCH",
        "/api/admin/settings",
        {"instructions": "Use CAM carefully", "default_timeout_ms": 45000},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["settings"]["server_instructions"] == "Use CAM carefully"
    assert payload["settings"]["instructions"] == "Use CAM carefully"  # deprecated alias
    assert payload["effective"]["server_instructions"] == "Use CAM carefully"
    assert payload["effective"]["default_timeout_s"] == 45

    # stored under the canonical key; the legacy key is gone
    await db_session.refresh(org)
    assert org.settings["server_instructions"] == "Use CAM carefully"
    assert org.settings["default_timeout_s"] == 45
    assert "instructions" not in org.settings
    assert "default_timeout_ms" not in org.settings

    # the GET endpoint surfaces the effective block too
    fetched = await admin_env.client.get("/api/admin/settings")
    assert fetched.status_code == 200
    assert fetched.json()["effective"]["server_instructions"] == "Use CAM carefully"
    assert fetched.json()["effective"]["default_timeout_s"] == 45


async def test_gateway_reads_server_instructions(
    admin_env: UserEnv,
    user_password: str,
) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await mutate_json(
        admin_env.client,
        "PATCH",
        "/api/admin/settings",
        {"server_instructions": "Gateway note"},
    )
    assert response.status_code == 200, response.text

    instructions = await load_server_instructions(admin_env.app.state.session_factory)
    assert instructions == "Gateway note"
