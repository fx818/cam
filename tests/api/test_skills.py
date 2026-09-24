"""Dashboard skills routes: browse, editor/zip saves, versions, rollback, shares."""

from __future__ import annotations

import base64
import io
import uuid
import zipfile
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import UserEnv, login, mutate_json, user_client
from cam.db.models import Service, User
from cam.registry.services import register_global_mcp


def _editor_skill(name: str, description: str, body: str = "Instructions.") -> dict[str, Any]:
    return {"name": name, "description": description, "requires": [], "body": body}


def _zip_bytes(files: dict[str, str]) -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for path, content in files.items():
            bundle.writestr(path, content)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


async def _create_skill(env_user: UserEnv, body: dict[str, Any]) -> dict[str, Any]:
    response = await mutate_json(env_user.client, "POST", "/api/skills", body)
    assert response.status_code < 500, response.text
    return response.json()


@pytest.fixture
async def restricted_global_service(
    db_session: AsyncSession,
) -> AsyncGenerator[Service]:
    suffix = uuid.uuid4().hex[:8]
    service, _connection = await register_global_mcp(
        db_session,
        slug=f"pns-{suffix}",
        name="PNS",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="static-header",
        header_name="X-Api-Key",
        credential="secret",
    )
    result = await db_session.execute(select(Service).where(Service.id == service.id))
    service_row = result.scalar_one()
    service_row.visibility = "restricted"
    await db_session.commit()
    yield service_row
    await db_session.rollback()


async def test_editor_skill_lifecycle(
    env_user: UserEnv, user_password: str, db_session: AsyncSession
) -> None:
    assert db_session is not None
    await login(env_user.client, env_user.user.email, user_password)
    created = await _create_skill(env_user, _editor_skill("PNS Analytics", "How to query PNS"))
    ref = created["skill"]["id"]
    assert created["skill"]["version"] == 1
    assert ref == f"{env_user.user.handle}/pns-analytics"

    listed = await env_user.client.get("/api/skills")
    mine = [e for e in listed.json()["skills"] if e["id"] == ref]
    assert mine and mine[0]["group"] == "mine" and mine[0]["available"] is True
    assert mine[0]["author"] == env_user.user.display_name

    detail = await env_user.client.get(f"/api/skills/{ref}")
    assert detail.status_code == 200
    assert detail.json()["skill"]["version"] == 1
    skill_md = next(f for f in detail.json()["skill"]["files"] if f["path"] == "SKILL.md")
    assert "name: PNS Analytics" in skill_md["content"]

    saved = await mutate_json(
        env_user.client,
        "POST",
        f"/api/skills/{ref}/versions",
        {**_editor_skill("PNS Analytics", "Updated description")},
    )
    assert saved.json()["version"]["version"] == 2
    detail = await env_user.client.get(f"/api/skills/{ref}")
    assert detail.json()["skill"]["version"] == 2

    deleted = await mutate_json(env_user.client, "DELETE", f"/api/skills/{ref}")
    assert deleted.status_code == 200
    gone = await env_user.client.get(f"/api/skills/{ref}")
    assert gone.status_code == 404


async def test_zip_upload_and_rejections(env_user: UserEnv, user_password: str) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    good = await mutate_json(
        env_user.client,
        "POST",
        "/api/skills",
        {
            "zip_base64": _zip_bytes(
                {"SKILL.md": "---\nname: Zip Skill\ndescription: from zip\n---\n\nbody\n"}
            )
        },
    )
    assert good.status_code == 200, good.text
    assert good.json()["skill"]["id"].endswith("/zip-skill")

    binary = await mutate_json(
        env_user.client,
        "POST",
        "/api/skills",
        {
            "zip_base64": _zip_bytes(
                {"SKILL.md": "---\nname: x\ndescription: d\n---\n", "run.sh": "#!"}
            )
        },
    )
    assert binary.status_code == 422
    assert "text files only" in binary.json()["error"]["message"]

    traversal = await mutate_json(
        env_user.client,
        "POST",
        "/api/skills",
        {
            "zip_base64": _zip_bytes(
                {"SKILL.md": "---\nname: t\ndescription: d\n---\n", "../evil.md": "nope"}
            )
        },
    )
    assert traversal.status_code == 422

    nowrap = await mutate_json(env_user.client, "POST", "/api/skills", {"zip_base64": "AAAA"})
    assert nowrap.status_code == 422


async def test_non_admin_cannot_create_global(env_user: UserEnv, user_password: str) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    response = await mutate_json(
        env_user.client,
        "POST",
        "/api/skills",
        {**_editor_skill("G", "d"), "tier": "global"},
    )
    assert response.status_code == 403


async def test_version_history_rollback(env_user: UserEnv, user_password: str) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    created = await _create_skill(
        env_user, {**_editor_skill("History Skill", "desc"), "body": "v1 body"}
    )
    ref = created["skill"]["id"]
    for expected in (2, 3):
        saved = await mutate_json(
            env_user.client,
            "POST",
            f"/api/skills/{ref}/versions",
            {"name": "History Skill", "description": "desc", "body": f"v{expected} body"},
        )
        assert saved.json()["version"]["version"] == expected

    roll = await mutate_json(env_user.client, "POST", f"/api/skills/{ref}/rollback", {"version": 1})
    assert roll.json()["version"]["version"] == 4
    detail = await env_user.client.get(f"/api/skills/{ref}")
    skill_md = next(f for f in detail.json()["skill"]["files"] if f["path"] == "SKILL.md")
    assert "v1 body" in skill_md["content"]
    assert detail.json()["skill"]["version"] == 4

    versions = await env_user.client.get(f"/api/skills/{ref}/versions")
    numbers = [v["version"] for v in versions.json()["versions"]]
    assert numbers == [4, 3, 2, 1]

    v1 = await env_user.client.get(f"/api/skills/{ref}/versions/1")
    assert "v1 body" in next(
        f["content"] for f in v1.json()["version"]["files"] if f["path"] == "SKILL.md"
    )


async def test_shares(
    env_user: UserEnv, user_password: str, second_user: User, second_password: str
) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    created = await _create_skill(env_user, _editor_skill("My Skill", "desc"))
    ref = created["skill"]["id"]

    shares = await mutate_json(
        env_user.client,
        "POST",
        f"/api/skills/{ref}/shares",
        {"email": second_user.email},
    )
    assert shares.status_code == 200
    share_id = shares.json()["share"]["id"]
    assert shares.json()["share"]["display_name"] == second_user.display_name

    async with user_client(env_user.app) as other:
        await login(other, second_user.email, second_password)
        listed = await other.get("/api/skills")
        entries = [e for e in listed.json()["skills"] if e["id"] == ref]
        assert entries and entries[0]["group"] == "shared"
        assert entries[0]["author"] == env_user.user.display_name

    revoked = await mutate_json(env_user.client, "DELETE", f"/api/skill-shares/{share_id}")
    assert revoked.status_code == 200

    async with user_client(env_user.app) as other:
        await login(other, second_user.email, second_password)
        listed = await other.get("/api/skills")
        assert all(e["id"] != ref for e in listed.json()["skills"])


async def test_non_owner_cannot_manage_shares(
    env_user: UserEnv, user_password: str, second_user: User, second_password: str
) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    created = await _create_skill(env_user, _editor_skill("Locked", "desc"))
    ref = created["skill"]["id"]
    async with user_client(env_user.app) as other:
        await login(other, second_user.email, second_password)
        response = await mutate_json(
            other, "POST", f"/api/skills/{ref}/shares", {"email": second_user.email}
        )
        assert response.status_code == 404


async def test_unavailable_shows_missing_access(
    env_user: UserEnv,
    user_password: str,
    restricted_global_service: Service,
) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    slug = restricted_global_service.slug
    created = await _create_skill(
        env_user,
        {
            "name": "Needs PNS",
            "description": "d",
            "requires": [f"global/{slug}"],
            "body": "text",
        },
    )
    ref = created["skill"]["id"]
    listed = await env_user.client.get("/api/skills")
    entry = next(e for e in listed.json()["skills"] if e["id"] == ref)
    assert entry["available"] is False
    assert f"requires global/{slug} (no access)" in entry["unavailable_reason"]
