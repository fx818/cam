"""R39: avoid avoidable 500s on valid input.

- an explicit JSON null for a non-nullable service patch field must be a 422,
  not a TypeError-driven 500;
- malformed keyset cursors (bad timestamp or UUID) must be a 422, not a 500.
"""

from __future__ import annotations

from api.helpers import UserEnv, login, mutate_json
from cam.api.pagination import encode_cursor
from cam.db.models import Connection, Service


async def test_patch_service_rejects_null_for_non_nullable_fields(
    env_user: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
) -> None:
    service, _connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    ref = f"{env_user.user.handle}/{service.slug}"

    for field in ("name", "description", "timeout_ms", "response_cap_tokens", "status"):
        response = await mutate_json(
            env_user.client, "PATCH", f"/api/services/{ref}", {field: None}
        )
        assert response.status_code == 422, (field, response.text)
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_patch_service_valid_field_still_works(
    env_user: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
) -> None:
    service, _connection = personal_service
    await login(env_user.client, env_user.user.email, user_password)
    ref = f"{env_user.user.handle}/{service.slug}"

    response = await mutate_json(
        env_user.client, "PATCH", f"/api/services/{ref}", {"timeout_ms": 30_000}
    )
    assert response.status_code == 200, response.text
    assert response.json()["service"]["timeout_ms"] == 30_000


async def test_audit_bad_cursor_is_422(env_user: UserEnv, user_password: str) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    bad = encode_cursor("not-a-timestamp", "not-a-uuid")
    response = await env_user.client.get("/api/audit", params={"cursor": bad})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_tokens_bad_cursor_is_422(env_user: UserEnv, user_password: str) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    bad = encode_cursor("2020-01-01T00:00:00+00:00", "not-a-uuid")
    response = await env_user.client.get("/api/tokens", params={"cursor": bad})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_audit_garbage_cursor_is_422(env_user: UserEnv, user_password: str) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    response = await env_user.client.get("/api/audit", params={"cursor": "!!!not-base64!!!"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
