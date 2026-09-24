from __future__ import annotations

import uuid

import pytest

from api.helpers import UserEnv, login, mutate_json
from cam.auth.sessions import CSRF_COOKIE, SESSION_COOKIE, issue_session, load_session


async def test_login_sets_session_and_csrf_cookies(env_user: UserEnv, user_password: str) -> None:
    response = await login(env_user.client, env_user.user.email, user_password)
    assert response.status_code == 200
    payload = response.json()
    assert payload["user"]["email"] == env_user.user.email
    assert payload["user"]["role"] == "user"
    assert payload["org"]["slug"] == "indiamart"
    assert str(payload["base_url"]).startswith("http")
    set_cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in set_cookies if c.startswith(f"{SESSION_COOKIE}="))
    csrf_cookie = next(c for c in set_cookies if c.startswith(f"{CSRF_COOKIE}="))
    assert "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    me = await env_user.client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == env_user.user.email


async def test_me_requires_session(env_user: UserEnv) -> None:
    response = await env_user.client.get("/api/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_get_request_issues_csrf_cookie_when_missing(env_user: UserEnv) -> None:
    assert env_user.client.cookies.get(CSRF_COOKIE) is None
    await env_user.client.get("/api/health")
    assert env_user.client.cookies.get(CSRF_COOKIE) is not None


async def test_login_rejects_missing_csrf(env_user: UserEnv) -> None:
    response = await env_user.client.post(
        "/api/auth/login", json={"email": "a@example.com", "password": "x"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_login_rejects_wrong_password(env_user: UserEnv) -> None:
    response = await login(env_user.client, env_user.user.email, "wrong-password")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_login_rejects_unknown_user(env_user: UserEnv) -> None:
    response = await login(env_user.client, "nobody@example.com", "whatever")
    assert response.status_code == 401


async def test_logout_clears_session(env_user: UserEnv, user_password: str) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    response = await mutate_json(env_user.client, "POST", "/api/auth/logout")
    assert response.status_code == 200
    me = await env_user.client.get("/api/auth/me")
    assert me.status_code == 401


async def test_account_lockout_after_five_failures(env_user: UserEnv, user_password: str) -> None:
    for _ in range(5):
        response = await login(env_user.client, env_user.user.email, "wrong-password")
        assert response.status_code == 401
    locked = await login(env_user.client, env_user.user.email, user_password)
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "RATE_LIMITED"


async def test_ip_rate_limit_across_emails(env_user: UserEnv) -> None:
    for index in range(20):
        response = await login(
            env_user.client, f"probe-{index}-{uuid.uuid4().hex[:6]}@example.com", "nope"
        )
        assert response.status_code == 401
    blocked = await login(env_user.client, f"fresh-{uuid.uuid4().hex[:6]}@example.com", "nope")
    assert blocked.status_code == 429


async def test_mutating_requires_csrf_header(env_user: UserEnv, user_password: str) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    response = await env_user.client.post("/api/auth/logout")
    assert response.status_code == 403


async def test_session_token_tamper_rejected(env_user: UserEnv, user_password: str) -> None:
    await login(env_user.client, env_user.user.email, user_password)
    raw = env_user.client.cookies.get(SESSION_COOKIE)
    assert raw is not None
    env_user.client.cookies.set(SESSION_COOKIE, raw + "x")
    me = await env_user.client.get("/api/auth/me")
    assert me.status_code == 401


def test_issue_and_load_session_roundtrip() -> None:
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    raw = issue_session("secret", user_id=user_id, org_id=org_id, now=1_000_000)
    payload = load_session("secret", raw, max_age=100, now=1_000_010)
    assert payload is not None
    assert payload.user_id == user_id
    assert payload.org_id == org_id
    assert load_session("secret", raw, max_age=100, now=1_000_200) is None
    assert load_session("other-secret", raw, max_age=100, now=1_000_010) is None
    assert load_session("secret", "garbage", max_age=100, now=1_000_010) is None


@pytest.mark.parametrize("bad", ["", "not-a-token"])
def test_load_session_rejects_junk(bad: str) -> None:
    assert load_session("secret", bad) is None
