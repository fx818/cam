from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import UserEnv, login, mutate_json, user_client
from cam.db.models import Connection, Service, Share, Tool, User
from cam.registry.services import register_personal_mcp
from cam.upstream import ConnectionTarget, UpstreamSession
from fixtures.mock_mcp import MockMCPHandle


async def test_services_list_groups(
    env_user: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
    global_service: tuple[Service, Connection],
    template_service: Service,
) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    response = await env_user.client.get("/api/services")
    assert response.status_code == 200
    services = response.json()["services"]
    by_group: dict[str, list[dict[str, object]]] = {}
    for entry in services:
        by_group.setdefault(entry["group"], []).append(entry)

    personal, _ = personal_service
    global_svc, _ = global_service
    assert any(e["id"] == f"global/{global_svc.slug}" for e in by_group.get("global", []))
    assert any(
        e["id"] == f"{env_user.user.handle}/{personal.slug}" for e in by_group.get("mine", [])
    )
    assert any(e["id"] == f"global/{template_service.slug}" for e in by_group.get("templates", []))
    template_entry = next(
        e for e in by_group["templates"] if e["id"] == f"global/{template_service.slug}"
    )
    assert template_entry["connection"] is None


async def test_service_detail_owner(
    env_user: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
) -> None:
    service, _connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    response = await env_user.client.get(f"/api/services/{env_user.user.handle}/{service.slug}")
    assert response.status_code == 200
    body = response.json()
    assert body["service"]["id"] == f"{env_user.user.handle}/{service.slug}"
    assert body["service"]["tier"] == "personal"
    assert body["viewer"]["is_owner"] is True
    assert body["viewer"]["can_manage"] is True
    assert body["connection"]["status"] == "connected"


async def test_service_detail_stranger_404(
    env_user: UserEnv,
    user_password: str,
    second_user: User,
    second_password: str,
    personal_service: tuple[Service, Connection],
) -> None:
    service, _connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    async with user_client(env_user.app) as other:
        await login(other, second_user.email, second_password)
        response = await other.get(f"/api/services/{env_user.user.handle}/{service.slug}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_shared_viewer_sees_service_detail(
    env_user: UserEnv,
    user_password: str,
    second_user: User,
    second_password: str,
    personal_service: tuple[Service, Connection],
    active_share: Share,
) -> None:
    service, _connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    async with user_client(env_user.app) as other:
        await login(other, second_user.email, second_password)
        response = await other.get(f"/api/services/{env_user.user.handle}/{service.slug}")
        assert response.status_code == 200
        body = response.json()
        assert body["viewer"]["is_owner"] is False
        assert body["viewer"]["is_shared"] is True
        assert body["viewer"]["can_manage"] is False


async def test_patch_personal_service(
    env_user: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
    db_session: AsyncSession,
) -> None:
    service, _connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    response = await mutate_json(
        env_user.client,
        "PATCH",
        f"/api/services/{env_user.user.handle}/{service.slug}",
        {"description": "updated", "timeout_ms": 30000},
    )
    assert response.status_code == 200
    assert response.json()["service"]["description"] == "updated"
    assert response.json()["service"]["timeout_ms"] == 30000

    await db_session.refresh(service)
    assert service.description == "updated"
    assert service.timeout_ms == 30000


async def test_patch_service_validation(
    env_user: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
) -> None:
    service, _connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    response = await mutate_json(
        env_user.client,
        "PATCH",
        f"/api/services/{env_user.user.handle}/{service.slug}",
        {"timeout_ms": 5},
    )
    assert response.status_code == 422


async def test_patch_global_service_requires_admin(
    env_user: UserEnv,
    user_password: str,
    global_service: tuple[Service, Connection],
) -> None:
    global_svc, _connection = global_service
    await login(env_user.client, env_user.user.email, user_password)
    response = await mutate_json(
        env_user.client,
        "PATCH",
        f"/api/services/global/{global_svc.slug}",
        {"description": "nope"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_admin_patches_global_service(
    admin_env: UserEnv,
    user_password: str,
    global_service: tuple[Service, Connection],
    db_session: AsyncSession,
) -> None:
    global_svc, _connection = global_service
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await mutate_json(
        admin_env.client,
        "PATCH",
        f"/api/services/global/{global_svc.slug}",
        {"description": "admin edit"},
    )
    assert response.status_code == 200
    await db_session.refresh(global_svc)
    assert global_svc.description == "admin edit"


async def test_delete_personal_service(
    env_user: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
    active_share: Share,
    db_session: AsyncSession,
) -> None:
    service, connection = personal_service
    ref = f"/api/services/{env_user.user.handle}/{service.slug}"
    await login(env_user.client, env_user.user.email, user_password)

    response = await mutate_json(env_user.client, "DELETE", ref)
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    after = await env_user.client.get(ref)
    assert after.status_code == 404

    await db_session.refresh(connection)
    await db_session.refresh(service)
    assert service.status == "deleted"
    assert connection.status == "disabled"
    assert connection.secret_enc is None

    await db_session.refresh(active_share)
    assert active_share.revoked_at is not None


async def test_tools_list_and_manage(
    env_user: UserEnv,
    user_password: str,
    second_user: User,
    second_password: str,
    personal_service: tuple[Service, Connection],
    personal_tool: Tool,
    active_share: Share,
) -> None:
    service, _connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    tools_path = f"/api/services/{env_user.user.handle}/{service.slug}/tools"

    response = await env_user.client.get(tools_path)
    assert response.status_code == 200
    body = response.json()
    assert body["can_manage"] is True
    assert len(body["tools"]) == 1
    assert body["tools"][0]["name"] == "echo"

    toggled = await mutate_json(
        env_user.client,
        "PATCH",
        f"/api/tools/{personal_tool.id}",
        {"enabled": False, "description_override": "Better description"},
    )
    assert toggled.status_code == 200
    assert toggled.json()["tool"]["enabled"] is False
    assert toggled.json()["tool"]["description_override"] == "Better description"

    after = await env_user.client.get(tools_path)
    owner_tools = after.json()["tools"]
    assert len(owner_tools) == 1
    assert owner_tools[0]["enabled"] is False

    async with user_client(env_user.app) as other:
        await login(other, second_user.email, second_password)
        shared = await other.get(tools_path)
        assert shared.status_code == 200
        assert shared.json()["tools"] == []
        assert shared.json()["can_manage"] is False


async def test_tools_hidden_from_stranger(
    env_user: UserEnv,
    user_password: str,
    second_user: User,
    second_password: str,
    personal_service: tuple[Service, Connection],
    personal_tool: Tool,
) -> None:
    service, _connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    async with user_client(env_user.app) as other:
        await login(other, second_user.email, second_password)
        response = await other.get(f"/api/services/{env_user.user.handle}/{service.slug}/tools")
        assert response.status_code == 404


async def test_stranger_cannot_patch_tool(
    env_user: UserEnv,
    user_password: str,
    second_user: User,
    second_password: str,
    personal_tool: Tool,
) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    async with user_client(env_user.app) as other:
        await login(other, second_user.email, second_password)
        response = await mutate_json(
            other, "PATCH", f"/api/tools/{personal_tool.id}", {"enabled": False}
        )
        assert response.status_code == 404


async def test_resync_endpoint_adds_personal_tool(
    env_user: UserEnv,
    user_password: str,
    db_session: AsyncSession,
    mock_mcp: MockMCPHandle,
    loopback_allowlist: object,
) -> None:
    owner = env_user.user
    handle = owner.handle
    service, _connection = await register_personal_mcp(
        db_session,
        owner=owner,
        slug=f"syncreal-{uuid.uuid4().hex[:8]}",
        name="Sync Real",
        url=mock_mcp.streamable_url,
        transport="streamable-http",
        auth_type="none",
    )
    await db_session.commit()
    await login(env_user.client, env_user.user.email, user_password)
    seeded = Tool(
        org_id=service.org_id,
        service_id=service.id,
        name="echo",
        upstream_name="echo",
        description="Seeded from the mock import",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        enabled=True,
        status="active",
    )
    db_session.add(seeded)
    await db_session.commit()
    dynamic_name = f"dynapi_{uuid.uuid4().hex[:8]}"
    probe = UpstreamSession(
        ConnectionTarget(
            service_id="probe3",
            connection_id=uuid.uuid4().hex,
            url=mock_mcp.streamable_url,
            transport="streamable-http",
        ),
        call_timeout_s=10.0,
    )
    await probe.start()
    try:
        outcome = await probe.call_tool("add_dynamic_tool", {"name": dynamic_name})
        assert outcome.is_error is False
    finally:
        await probe.aclose()
    response = await mutate_json(
        env_user.client, "POST", f"/api/services/{handle}/{service.slug}/resync"
    )
    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["resync"]["status"] == "done"
    assert dynamic_name in payload["resync"]["added"]
    listed = await env_user.client.get(f"/api/services/{handle}/{service.slug}/tools")
    assert listed.status_code == 200
    names = [tool["name"] for tool in listed.json()["tools"]]
    assert dynamic_name in names


async def test_resync_endpoint_forbidden_for_stranger(
    env_user: UserEnv,
    user_password: str,
    second_user: User,
    second_password: str,
    personal_service: tuple[Service, Connection],
) -> None:
    service, _connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    async with user_client(env_user.app) as other:
        await login(other, second_user.email, second_password)
        response = await mutate_json(
            other, "POST", f"/api/services/{env_user.user.handle}/{service.slug}/resync"
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_resync_endpoint_reports_failure(
    env_user: UserEnv, user_password: str, personal_service: tuple[Service, Connection]
) -> None:
    service, _connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    response = await mutate_json(
        env_user.client, "POST", f"/api/services/{env_user.user.handle}/{service.slug}/resync"
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "RESYNC_FAILED"


async def test_services_require_auth(env_user: UserEnv) -> None:
    response = await env_user.client.get("/api/services")
    assert response.status_code == 401
