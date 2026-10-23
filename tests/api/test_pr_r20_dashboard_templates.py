"""R20: dashboard template visibility now goes through the access module.

A `restricted` template is hidden unless the viewer has a visibility grant,
and a granted viewer sees it — behaviour must match the old inline query.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import UserEnv, login
from cam.db.models import Org, Service, ServiceVisibility


def _template(org: Org, slug: str, visibility: str) -> Service:
    return Service(
        org_id=org.id,
        tier="template",
        owner_user_id=None,
        slug=slug,
        name=slug,
        description="template",
        kind="mcp",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="none",
        auth_config={},
        visibility=visibility,
        status="active",
        health="unknown",
    )


async def test_restricted_template_hidden_until_granted(
    env_user: UserEnv,
    user_password: str,
    db_session: AsyncSession,
    org: Org,
) -> None:
    prefix = uuid.uuid4().hex[:8]
    visible = _template(org, f"vis-{prefix}", "all")
    hidden = _template(org, f"hid-{prefix}", "restricted")
    db_session.add_all([visible, hidden])
    await db_session.commit()

    await login(env_user.client, env_user.user.email, user_password)

    response = await env_user.client.get("/api/services")
    assert response.status_code == 200, response.text
    ids = {entry["id"] for entry in response.json()["services"]}
    assert f"global/vis-{prefix}" in ids
    assert f"global/hid-{prefix}" not in ids

    db_session.add(ServiceVisibility(org_id=org.id, service_id=hidden.id, user_id=env_user.user.id))
    await db_session.commit()

    response = await env_user.client.get("/api/services")
    ids = {entry["id"] for entry in response.json()["services"]}
    assert f"global/hid-{prefix}" in ids
