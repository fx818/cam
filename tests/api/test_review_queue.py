from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import UserEnv, login, mutate_json
from cam.db.models import Service, Tool


async def seed_pending(db_session: AsyncSession, service: Service, name: str) -> Tool:
    tool = Tool(
        org_id=service.org_id,
        service_id=service.id,
        name=name,
        upstream_name=name,
        description=f"Brand new {name}",
        input_schema={"type": "object", "properties": {}},
        enabled=False,
        status="pending_review",
    )
    db_session.add(tool)
    await db_session.commit()
    return tool


async def test_review_queue_requires_admin(env_user: UserEnv, user_password: str) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    response = await env_user.client.get("/api/admin/review-queue")
    assert response.status_code == 403


async def test_review_queue_requires_auth(env_user: UserEnv) -> None:
    response = await env_user.client.get("/api/admin/review-queue")
    assert response.status_code == 401


async def test_review_queue_lists_pending_global(
    admin_env: UserEnv,
    user_password: str,
    global_service: tuple[Service, object],
    db_session: AsyncSession,
) -> None:
    service, _connection = global_service
    global_tool = await seed_pending(db_session, service, "fresh_global")
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await admin_env.client.get("/api/admin/review-queue")
    entries = {entry["id"]: entry for entry in response.json()["tools"]}
    tool_id = str(global_tool.id)
    assert tool_id in entries
    assert entries[tool_id]["service_ref"] == f"global/{service.slug}"
    assert entries[tool_id]["tier"] == "global"
    assert entries[tool_id]["status"] == "pending_review"


async def test_review_queue_lists_pending_template(
    admin_env: UserEnv,
    user_password: str,
    template_service: Service,
    db_session: AsyncSession,
) -> None:
    template_tool = await seed_pending(db_session, template_service, "fresh_template")
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await admin_env.client.get("/api/admin/review-queue")
    entries = {entry["id"]: entry for entry in response.json()["tools"]}
    assert str(template_tool.id) in entries
    assert entries[str(template_tool.id)]["tier"] == "template"


async def test_review_queue_skips_personal_tools(
    admin_env: UserEnv,
    user_password: str,
    org: object,
    db_session: AsyncSession,
) -> None:
    del org
    from cam.accounts.users import create_user
    from cam.registry.services import register_personal_mcp

    owner = await create_user(
        db_session,
        email=f"rq-{uuid.uuid4().hex[:8]}@example.com",
        password="pw",
        role="user",
    )
    service, _connection = await register_personal_mcp(
        db_session,
        owner=owner,
        slug=f"rq-{uuid.uuid4().hex[:8]}",
        name="RQ Personal",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="none",
    )
    personal_tool = await seed_pending(db_session, service, "personal_pending")
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await admin_env.client.get("/api/admin/review-queue")
    entries = {entry["id"] for entry in response.json()["tools"]}
    assert str(personal_tool.id) not in entries


async def test_bulk_approve_enables_tools(
    admin_env: UserEnv,
    user_password: str,
    global_service: tuple[Service, object],
    db_session: AsyncSession,
) -> None:
    service, _connection = global_service
    one = await seed_pending(db_session, service, "approve_one")
    two = await seed_pending(db_session, service, "approve_two")
    missing = uuid.uuid4()
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await mutate_json(
        admin_env.client,
        "POST",
        "/api/admin/review-queue/approve",
        {"tool_ids": [str(one.id), str(two.id), str(missing)]},
    )
    assert response.status_code == 200, response.json()
    assert response.json()["approved"] == [str(one.id), str(two.id)]
    assert response.json()["unknown"] == [str(missing)]
    await db_session.refresh(one)
    await db_session.refresh(two)
    assert one.status == "active" and one.enabled is True
    assert two.status == "active" and two.enabled is True


async def test_bulk_approve_user_forbidden(
    env_user: UserEnv,
    user_password: str,
    global_service: tuple[Service, object],
    db_session: AsyncSession,
) -> None:
    service, _connection = global_service
    one = await seed_pending(db_session, service, "user_try")
    await login(env_user.client, env_user.user.email, user_password)
    response = await mutate_json(
        env_user.client, "POST", "/api/admin/review-queue/approve", {"tool_ids": [str(one.id)]}
    )
    assert response.status_code == 403
    await db_session.refresh(one)
    assert one.status == "pending_review"


async def test_bulk_approve_empty_422(admin_env: UserEnv, user_password: str) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await mutate_json(
        admin_env.client, "POST", "/api/admin/review-queue/approve", {"tool_ids": []}
    )
    assert response.status_code == 422


async def test_bulk_approve_no_pending_404(admin_env: UserEnv, user_password: str) -> None:
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await mutate_json(
        admin_env.client,
        "POST",
        "/api/admin/review-queue/approve",
        {"tool_ids": [str(uuid.uuid4())]},
    )
    assert response.status_code == 404
