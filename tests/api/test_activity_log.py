from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import UserEnv, insert_audit, login, user_client
from cam.db.models import Connection, Org, Service, User


async def test_audit_scopes(
    env_user: UserEnv,
    user_password: str,
    second_user: User,
    org: Org,
    personal_service: tuple[Service, Connection],
    db_session: AsyncSession,
) -> None:
    service, _connection = personal_service
    now = datetime.now(UTC)
    own = await insert_audit(
        db_session,
        org_id=org.id,
        user_id=env_user.user.id,
        owner_user_id=env_user.user.id,
        service_id=service.id,
        agent_name="claude-code",
        args={"query": "select 1"},
        ts=now - timedelta(minutes=5),
    )
    await insert_audit(
        db_session,
        org_id=org.id,
        user_id=second_user.id,
        owner_user_id=env_user.user.id,
        service_id=service.id,
        agent_name="codex",
        ts=now - timedelta(minutes=4),
    )
    await insert_audit(
        db_session,
        org_id=org.id,
        user_id=second_user.id,
        owner_user_id=second_user.id,
        agent_name="cursor",
        ts=now - timedelta(minutes=3),
    )
    await db_session.commit()

    await login(env_user.client, env_user.user.email, user_password)

    both = await env_user.client.get("/api/audit")
    assert both.status_code == 200
    entries = both.json()["entries"]
    assert len(entries) == 2
    assert {e["agent_name"] for e in entries} == {"claude-code", "codex"}

    mine = await env_user.client.get("/api/audit", params={"scope": "mine"})
    assert len(mine.json()["entries"]) == 1
    assert mine.json()["entries"][0]["agent_name"] == "claude-code"

    owned = await env_user.client.get("/api/audit", params={"scope": "my_services"})
    assert len(owned.json()["entries"]) == 2

    entry_ids = {e["id"] for e in entries}
    assert str(own.id) in entry_ids


async def test_audit_filters(
    env_user: UserEnv,
    user_password: str,
    org: Org,
    personal_service: tuple[Service, Connection],
    db_session: AsyncSession,
) -> None:
    service, _connection = personal_service
    day = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    await insert_audit(
        db_session,
        org_id=org.id,
        user_id=env_user.user.id,
        owner_user_id=env_user.user.id,
        service_id=service.id,
        agent_name="claude-code",
        status="ok",
        ts=day,
    )
    await insert_audit(
        db_session,
        org_id=org.id,
        user_id=env_user.user.id,
        owner_user_id=env_user.user.id,
        service_id=service.id,
        agent_name="codex",
        status="error",
        ts=day - timedelta(days=2),
    )
    await db_session.commit()

    await login(env_user.client, env_user.user.email, user_password)

    by_agent = await env_user.client.get("/api/audit", params={"agent": "claude"})
    assert len(by_agent.json()["entries"]) == 1

    by_status = await env_user.client.get("/api/audit", params={"status": "error"})
    assert len(by_status.json()["entries"]) == 1
    assert by_status.json()["entries"][0]["agent_name"] == "codex"

    by_service = await env_user.client.get(
        "/api/audit",
        params={"service": f"{env_user.user.handle}/{service.slug}"},
    )
    assert len(by_service.json()["entries"]) == 2

    unknown_service = await env_user.client.get(
        "/api/audit", params={"service": "global/does-not-exist"}
    )
    assert unknown_service.json()["entries"] == []

    start = (day - timedelta(days=1)).date().isoformat()
    end = day.date().isoformat()
    by_date = await env_user.client.get("/api/audit", params={"from": start, "to": end})
    assert len(by_date.json()["entries"]) == 1
    assert by_date.json()["entries"][0]["agent_name"] == "claude-code"


async def test_audit_cursor_pagination(
    env_user: UserEnv,
    user_password: str,
    org: Org,
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    for index in range(3):
        await insert_audit(
            db_session,
            org_id=org.id,
            user_id=env_user.user.id,
            agent_name=f"agent-{index}",
            ts=now - timedelta(minutes=index),
        )
    await db_session.commit()

    await login(env_user.client, env_user.user.email, user_password)

    first = await env_user.client.get("/api/audit", params={"limit": 2})
    body = first.json()
    assert len(body["entries"]) == 2
    assert body["next_cursor"] is not None

    second = await env_user.client.get(
        "/api/audit", params={"limit": 2, "cursor": body["next_cursor"]}
    )
    second_body = second.json()
    assert len(second_body["entries"]) == 1
    assert second_body["next_cursor"] is None

    agents = [e["agent_name"] for e in body["entries"] + second_body["entries"]]
    assert agents == ["agent-0", "agent-1", "agent-2"]


async def test_audit_detail_access(
    env_user: UserEnv,
    user_password: str,
    second_user: User,
    second_password: str,
    org: Org,
    db_session: AsyncSession,
) -> None:
    own = await insert_audit(
        db_session,
        org_id=org.id,
        user_id=env_user.user.id,
        owner_user_id=env_user.user.id,
        agent_name="claude-code",
        args={"path": "/tmp/x", "depth": 2},
    )
    foreign = await insert_audit(
        db_session,
        org_id=org.id,
        user_id=second_user.id,
        owner_user_id=second_user.id,
        agent_name="cursor",
        args={"secret": "not-yours"},
    )
    await db_session.commit()

    await login(env_user.client, env_user.user.email, user_password)

    detail = await env_user.client.get(f"/api/audit/{own.id}")
    assert detail.status_code == 200
    assert detail.json()["entry"]["args"] == {"path": "/tmp/x", "depth": 2}

    denied = await env_user.client.get(f"/api/audit/{foreign.id}")
    assert denied.status_code == 404

    async with user_client(env_user.app) as other:
        await login(other, second_user.email, second_password)
        theirs = await other.get(f"/api/audit/{foreign.id}")
        assert theirs.status_code == 200
        not_theirs = await other.get(f"/api/audit/{own.id}")
        assert not_theirs.status_code == 404


async def test_audit_requires_auth(env_user: UserEnv) -> None:
    response = await env_user.client.get("/api/audit")
    assert response.status_code == 401


async def test_skills_list_requires_auth(env_user: UserEnv) -> None:
    response = await env_user.client.get("/api/skills")
    assert response.status_code == 401


async def test_skills_list_empty(
    env_user: UserEnv,
    user_password: str,
) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    response = await env_user.client.get("/api/skills")
    assert response.status_code == 200
    assert response.json() == {"skills": [], "next_cursor": None}
