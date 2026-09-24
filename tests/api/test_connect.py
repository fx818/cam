"""Phase 3.5 routes: connect secret services and the OAuth authorize flow."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.helpers import ApiEnv, ensure_csrf, post_json
from cam.config import Settings
from cam.db.models import Connection, OAuthState, Service
from fixtures.mock_oauth import MockOAuthHandle
from fixtures.mock_rest import MockRestHandle


def _payload(response: Any) -> dict[str, Any]:
    body: Any = response.json()
    assert isinstance(body, dict)
    return cast("dict[str, Any]", body)


def _qs(url: str) -> dict[str, list[str]]:
    raw = parse_qs(urlparse(url).query)
    return {key: [str(item) for item in values] for key, values in raw.items()}


async def test_connect_stores_bearer_credential(
    api: ApiEnv, user_token: str, db_session: AsyncSession
) -> None:
    create = await post_json(
        api.client,
        "/api/services",
        {
            "slug": f"conn-{uuid.uuid4().hex[:8]}",
            "name": "Connect REST",
            "url": "https://api.example.com",
            "kind": "rest",
            "auth_type": "bearer",
            "credential": "placeholder",
        },
        token=user_token,
    )
    assert create.status_code == 200
    ref = str(_payload(create)["service"]["id"])
    owner, slug = ref.split("/", 1)

    response = await post_json(
        api.client,
        f"/api/services/{owner}/{slug}/connect",
        {"credential": "connect-secret"},
        token=user_token,
    )
    assert response.status_code == 200
    payload = _payload(response)
    connection = cast("dict[str, Any]", payload["connection"])
    assert connection["status"] == "connected"
    connection_id = str(connection["id"])

    row = (
        await db_session.execute(
            select(Connection).where(Connection.id == uuid.UUID(connection_id))
        )
    ).scalar_one()
    assert row.secret_enc is not None
    assert b"connect-secret" not in row.secret_enc


async def test_connect_requires_credential_for_secret_types(api: ApiEnv, user_token: str) -> None:
    create = await post_json(
        api.client,
        "/api/services",
        {
            "slug": f"conn-{uuid.uuid4().hex[:8]}",
            "name": "Needs Secret",
            "url": "https://api.example.com",
            "kind": "rest",
            "auth_type": "bearer",
            "credential": "placeholder",
        },
        token=user_token,
    )
    assert create.status_code == 200
    ref = str(_payload(create)["service"]["id"])
    owner, slug = ref.split("/", 1)
    response = await post_json(
        api.client, f"/api/services/{owner}/{slug}/connect", {}, token=user_token
    )
    assert response.status_code == 422
    assert _payload(response)["error"]["code"] == "VALIDATION_ERROR"


async def test_connect_requires_auth(api: ApiEnv) -> None:
    csrf = await ensure_csrf(api.client)
    response = await api.client.post(
        "/api/services/global/nope/connect", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 401


async def test_connect_unknown_service_is_404(api: ApiEnv, user_token: str) -> None:
    response = await post_json(
        api.client, "/api/services/global/does-not-exist/connect", {}, token=user_token
    )
    assert response.status_code == 404


async def test_connect_hidden_from_non_owner(
    api: ApiEnv, user_token: str, db_session: AsyncSession
) -> None:
    from cam.accounts.users import create_user
    from cam.connections.tokens import create_token

    suffix = uuid.uuid4().hex[:10]
    other = await create_user(
        db_session,
        email=f"other-{suffix}@example.com",
        password=f"pw-{suffix}",
        role="user",
    )
    other_token, _ = await create_token(
        db_session, org_id=other.org_id, user_id=other.id, label="other"
    )
    await db_session.commit()

    create = await post_json(
        api.client,
        "/api/services",
        {
            "slug": f"priv-{uuid.uuid4().hex[:8]}",
            "name": "Private",
            "url": "https://api.example.com",
            "kind": "rest",
            "auth_type": "bearer",
            "credential": "x",
        },
        token=user_token,
    )
    assert create.status_code == 200
    ref = str(_payload(create)["service"]["id"])
    owner, slug = ref.split("/", 1)
    response = await post_json(
        api.client, f"/api/services/{owner}/{slug}/connect", {"credential": "y"}, token=other_token
    )
    assert response.status_code == 404


async def test_oauth_connect_returns_authorize_url(
    api: ApiEnv,
    user_token: str,
    mock_rest: MockRestHandle,
    mock_oauth: MockOAuthHandle,
    loopback_allowlist: None,
) -> None:
    create = await post_json(
        api.client,
        "/api/services",
        {
            "slug": f"oauth-{uuid.uuid4().hex[:8]}",
            "name": "OAuth REST",
            "url": mock_rest.base_url,
            "kind": "rest",
            "auth_type": "oauth",
            "spec_url": mock_rest.spec_url,
        },
        token=user_token,
    )
    assert create.status_code == 200
    ref = str(_payload(create)["service"]["id"])
    owner, slug = ref.split("/", 1)

    response = await post_json(
        api.client, f"/api/services/{owner}/{slug}/connect", {}, token=user_token
    )
    assert response.status_code == 200
    authorize_url = str(_payload(response)["authorize_url"])
    query = _qs(authorize_url)
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [f"{Settings().base_url}/api/oauth/callback"]
    assert query.get("state")

    state = query["state"][0]
    async with api.app.state.session_factory() as db:
        row = (
            await db.execute(select(OAuthState).where(OAuthState.state == state))
        ).scalar_one_or_none()
        assert row is not None
        assert row.pkce_verifier


async def test_oauth_callback_stores_tokens_and_redirects(
    api: ApiEnv,
    user_token: str,
    mock_rest: MockRestHandle,
    mock_oauth: MockOAuthHandle,
    loopback_allowlist: None,
    db_session: AsyncSession,
) -> None:
    create = await post_json(
        api.client,
        "/api/services",
        {
            "slug": f"oauth-{uuid.uuid4().hex[:8]}",
            "name": "OAuth REST",
            "url": mock_rest.base_url,
            "kind": "rest",
            "auth_type": "oauth",
            "spec_url": mock_rest.spec_url,
        },
        token=user_token,
    )
    assert create.status_code == 200
    ref = str(_payload(create)["service"]["id"])
    owner, slug = ref.split("/", 1)

    connect = await post_json(
        api.client, f"/api/services/{owner}/{slug}/connect", {}, token=user_token
    )
    assert connect.status_code == 200
    authorize_url = str(_payload(connect)["authorize_url"])
    sep = "&" if "?" in authorize_url else "?"
    async with httpx.AsyncClient(timeout=10.0) as external:
        authorize_resp = await external.get(
            f"{authorize_url}{sep}as_user=alice",
            follow_redirects=False,
        )
    assert authorize_resp.status_code == 302
    loc_query = _qs(authorize_resp.headers["location"])
    code = loc_query["code"][0]
    state = loc_query["state"][0]

    callback = await api.client.get(
        f"/api/oauth/callback?code={code}&state={state}", follow_redirects=False
    )
    assert callback.status_code == 302
    assert callback.headers["location"] == f"{Settings().base_url.rstrip('/')}/"

    result = await db_session.execute(
        select(Connection).where(
            Connection.user_id.is_not(None),
            Connection.expires_at.is_not(None),
            Connection.secret_enc.is_not(None),
        )
    )
    oauth_connections = list(result.scalars().all())
    assert oauth_connections
    token_row = oauth_connections[-1]
    assert token_row.status == "connected"
    assert any(token.startswith("tok-alice-") for token in mock_oauth.bearer_tokens)

    remaining = (
        await db_session.execute(select(OAuthState).where(OAuthState.state == state))
    ).scalar_one_or_none()
    assert remaining is None


async def test_oauth_callback_unknown_state_is_404(api: ApiEnv) -> None:
    response = await api.client.get(
        "/api/oauth/callback?code=x&state=missing-state", follow_redirects=False
    )
    assert response.status_code == 404


async def test_oauth_callback_requires_code_and_state(api: ApiEnv) -> None:
    response = await api.client.get("/api/oauth/callback", follow_redirects=False)
    assert response.status_code == 422


async def test_connect_global_needs_admin(api: ApiEnv, user_token: str) -> None:
    from cam.registry.services import register_global_mcp

    slug = f"glob{uuid.uuid4().hex[:8]}"
    async with api.app.state.session_factory() as db:
        await register_global_mcp(
            db,
            slug=slug,
            name="Global Bearer",
            url="https://mcp.example.com/mcp",
            transport="streamable-http",
            auth_type="bearer",
            credential="org-secret",
        )
        await db.commit()

    response = await post_json(
        api.client, f"/api/services/global/{slug}/connect", {"credential": "new"}, token=user_token
    )
    assert response.status_code == 403
    assert _payload(response)["error"]["code"] == "FORBIDDEN"


async def test_oauth_refresh_failure_marks_needs_reconnect(
    api: ApiEnv,
    user_token: str,
    mock_rest: MockRestHandle,
    mock_oauth: MockOAuthHandle,
    loopback_allowlist: None,
    db_session: AsyncSession,
) -> None:
    from cam.access.credentials import CredentialExpiredError, resolve_credential

    create = await post_json(
        api.client,
        "/api/services",
        {
            "slug": f"oauth-{uuid.uuid4().hex[:8]}",
            "name": "OAuth Refresh",
            "url": mock_rest.base_url,
            "kind": "rest",
            "auth_type": "oauth",
            "spec_url": mock_rest.spec_url,
        },
        token=user_token,
    )
    assert create.status_code == 200
    ref = str(_payload(create)["service"]["id"])
    owner, slug = ref.split("/", 1)

    connect = await post_json(
        api.client, f"/api/services/{owner}/{slug}/connect", {}, token=user_token
    )
    authorize_url = str(_payload(connect)["authorize_url"])
    sep = "&" if "?" in authorize_url else "?"
    async with httpx.AsyncClient(timeout=10.0) as external:
        authorize_resp = await external.get(
            f"{authorize_url}{sep}as_user=bob",
            follow_redirects=False,
        )
    assert authorize_resp.status_code == 302
    loc_query = _qs(authorize_resp.headers["location"])
    callback = await api.client.get(
        f"/api/oauth/callback?code={loc_query['code'][0]}&state={loc_query['state'][0]}",
        follow_redirects=False,
    )
    assert callback.status_code == 302

    result = await db_session.execute(
        select(Connection).where(Connection.expires_at.is_not(None)).order_by(Connection.created_at)
    )
    connection = list(result.scalars().all())[-1]
    connection.expires_at = datetime.now(UTC) + timedelta(seconds=30)
    await db_session.commit()

    mock_oauth.set_refresh_fail = True

    service_row = await db_session.get(Service, connection.service_id)
    assert service_row is not None
    try:
        await resolve_credential(db_session, service=service_row, connection=connection)
        raise AssertionError("expected CredentialExpiredError")
    except CredentialExpiredError:
        pass
    await db_session.refresh(connection)
    assert connection.status == "needs_reconnect"
    await db_session.refresh(service_row)
    assert service_row.health == "needs_reconnect"
