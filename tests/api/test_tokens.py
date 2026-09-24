from __future__ import annotations

from api.helpers import UserEnv, login, mutate_json
from cam.db.models import User


async def test_tokens_require_auth(env_user: UserEnv) -> None:
    response = await env_user.client.get("/api/tokens")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_token_create_list_delete(env_user: UserEnv, user_password: str) -> None:
    await login(env_user.client, env_user.user.email, user_password)

    empty = await env_user.client.get("/api/tokens")
    assert empty.status_code == 200
    assert empty.json() == {"tokens": [], "next_cursor": None}

    created = await mutate_json(env_user.client, "POST", "/api/tokens", {"label": "rahul-laptop"})
    assert created.status_code == 201
    payload = created.json()
    raw = payload["token"]
    assert isinstance(raw, str) and raw.startswith("cam_")
    assert payload["label"] == "rahul-laptop"
    assert payload["token_prefix"]
    assert payload["revoked_at"] is None

    listed = await env_user.client.get("/api/tokens")
    tokens = listed.json()["tokens"]
    assert len(tokens) == 1
    assert "token" not in tokens[0]
    assert tokens[0]["label"] == "rahul-laptop"

    token_id = payload["id"]
    deleted = await mutate_json(env_user.client, "DELETE", f"/api/tokens/{token_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}

    listed_again = await env_user.client.get("/api/tokens")
    tokens_again = listed_again.json()["tokens"]
    assert len(tokens_again) == 1
    assert tokens_again[0]["revoked_at"] is not None


async def test_token_create_requires_label(env_user: UserEnv, user_password: str) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    response = await mutate_json(env_user.client, "POST", "/api/tokens", {"label": ""})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_delete_other_users_token_404(
    env_user: UserEnv, user_password: str, second_user: User, second_password: str
) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    created = await mutate_json(env_user.client, "POST", "/api/tokens", {"label": "mine"})
    mine_id = created.json()["id"]

    from api.helpers import user_client

    async with user_client(env_user.app) as other:
        await login(other, second_user.email, second_password)
        stolen = await mutate_json(other, "DELETE", f"/api/tokens/{mine_id}")
        assert stolen.status_code == 404
        theirs = await mutate_json(other, "POST", "/api/tokens", {"label": "theirs"})
        assert theirs.status_code == 201

    still_there = await env_user.client.get("/api/tokens")
    labels = [t["label"] for t in still_there.json()["tokens"] if t["revoked_at"] is None]
    assert "mine" in labels


async def test_token_cursor_pagination(env_user: UserEnv, user_password: str) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    for index in range(3):
        response = await mutate_json(
            env_user.client, "POST", "/api/tokens", {"label": f"tok-{index}"}
        )
        assert response.status_code == 201

    first = await env_user.client.get("/api/tokens", params={"limit": 2})
    assert first.status_code == 200
    body = first.json()
    assert len(body["tokens"]) == 2
    assert body["next_cursor"] is not None

    second = await env_user.client.get(
        "/api/tokens", params={"limit": 2, "cursor": body["next_cursor"]}
    )
    second_body = second.json()
    assert len(second_body["tokens"]) == 1
    assert second_body["next_cursor"] is None

    invalid = await env_user.client.get("/api/tokens", params={"cursor": "not-a-cursor"})
    assert invalid.status_code == 422
