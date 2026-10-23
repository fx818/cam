"""R22: the "explain failure" endpoint must query AuditLog by (org_id, id).

AuditLog's primary key is composite `(id, ts)`, so `session.get(AuditLog, id)`
raises `InvalidRequestError` and the endpoint 500s. It must instead use an
explicit query filtered by org_id + id.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import UserEnv, insert_audit, login, post_json
from cam.db.models import Org


async def test_explain_audit_entry_does_not_500(
    admin_env: UserEnv,
    user_password: str,
    db_session: AsyncSession,
    org: Org,
) -> None:
    entry = await insert_audit(
        db_session,
        org_id=org.id,
        user_id=admin_env.user.id,
        owner_user_id=admin_env.user.id,
        agent_name="claude-code",
        action="invoke",
        status="error",
        args={"query": "select 1"},
    )
    await db_session.commit()

    await login(admin_env.client, admin_env.user.email, user_password)
    response = await post_json(admin_env.client, f"/api/admin/audit/{entry.id}/explain", {})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entry_id"] == str(entry.id)
    stages = {stage["stage"] for stage in body["explain"]}
    assert "logged_error" in stages
    assert "note" in stages


async def test_explain_unknown_entry_returns_404(
    admin_env: UserEnv,
    user_password: str,
) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await post_json(admin_env.client, f"/api/admin/audit/{uuid.uuid4()}/explain", {})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
