"""Phase 3 done-when, end to end through /mcp.

A REST API imported from OpenAPI works end to end, and two users each connect
their own credentials through ONE OAuth template and invoke through CAM with
distinct bearer tokens.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from cam.api.main import create_app
from cam.config import Settings
from cam.connections.orgs import DEFAULT_ORG_SLUG, ensure_org
from cam.connections.tokens import create_token
from cam.connections.url_policy import AllowlistError, add_url_allowlist, remove_url_allowlist
from cam.db.models import Connection, User
from cam.db.session import create_db_engine, create_session_factory
from fixtures.mock_oauth import MockOAuthHandle
from fixtures.mock_rest import API_KEY_NAME, API_KEY_VALUE, MockRestHandle
from gateway.helpers import McpConnection, make_result_payload, make_result_text


async def _remove_allowlist() -> None:
    engine = create_db_engine(Settings().database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            try:
                await remove_url_allowlist(
                    session, pattern="127.0.0.0/8", org_slug=DEFAULT_ORG_SLUG
                )
                await session.commit()
            except Exception:
                await session.rollback()
    finally:
        await engine.dispose()


def _user(org_id: uuid.UUID, suffix: str, name: str) -> User:
    return User(
        org_id=org_id,
        email=f"dw3-{name}-{suffix}@example.com",
        handle=f"dw3{name}{suffix}",
        display_name=f"DoneWhen3 {name.title()}",
        password_hash="x",
        role="user",
        kind="person",
    )


@asynccontextmanager
async def _running_app(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    stop = asyncio.Event()
    started = asyncio.Event()
    failures: list[BaseException] = []

    async def _run_lifespan() -> None:
        try:
            async with app.router.lifespan_context(app):
                started.set()
                await stop.wait()
        except BaseException as exc:
            failures.append(exc)
            started.set()
            raise

    task = asyncio.create_task(_run_lifespan(), name="cam-dw3-lifespan")
    await started.wait()
    if failures:
        await task
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway.test", timeout=30.0
    ) as client:
        yield client
    stop.set()
    await task


async def _fetch_csrf(api: httpx.AsyncClient) -> str:
    api.cookies.jar.clear()
    await api.get("/api/health")
    values = [cookie.value for cookie in api.cookies.jar if cookie.name == "cam_csrf"]
    assert values, "no CSRF cookie issued"
    value = values[-1]
    assert value is not None
    return value


async def _connect_template(
    api: httpx.AsyncClient, token: str, connect_path: str, *, as_user: str
) -> None:
    csrf = await _fetch_csrf(api)
    connect = await api.post(
        connect_path,
        json={},
        headers={"Authorization": f"Bearer {token}", "X-CSRF-Token": csrf},
    )
    assert connect.status_code == 200, connect.text
    authorize_url = connect.json()["authorize_url"]
    sep = "&" if "?" in authorize_url else "?"
    async with httpx.AsyncClient(timeout=10.0) as external:
        authorize = await external.get(
            f"{authorize_url}{sep}as_user={as_user}", follow_redirects=False
        )
    assert authorize.status_code == 302, authorize.text
    callback = await api.get(authorize.headers["location"], follow_redirects=False)
    assert callback.status_code == 302, callback.text


async def test_phase3_done_when_rest_openapi(mock_rest: MockRestHandle) -> None:
    try:
        suffix = uuid.uuid4().hex[:10]
        engine = create_db_engine(Settings().database_url)
        factory = create_session_factory(engine)
        try:
            async with factory() as session:
                org = await ensure_org(session)
                try:
                    allowlist_row = await add_url_allowlist(
                        session, pattern="127.0.0.0/8", org_slug=org.slug
                    )
                    del allowlist_row
                except AllowlistError:
                    await session.rollback()
                person = _user(org.id, suffix, "u")
                session.add(person)
                await session.flush()
                raw_token, _ = await create_token(
                    session, org_id=org.id, user_id=person.id, label="dw3-rest"
                )
                from cam.registry.services import register_service

                service, _conn = await register_service(
                    session,
                    tier="global",
                    slug=f"metamcp{suffix}",
                    name="Mock Meta",
                    url=mock_rest.base_url,
                    kind="rest",
                    auth_type="api-key",
                    api_key_name=API_KEY_NAME,
                    api_key_in="header",
                    credential=API_KEY_VALUE,
                    spec_url=mock_rest.spec_url,
                    tools=["listItems", "getItem"],
                )
                await session.commit()
                ref = f"global/{service.slug}"
        finally:
            await engine.dispose()

        mock_rest.requests.clear()
        app = create_app()
        async with _running_app(app) as client:
            connection = McpConnection(client, raw_token)
            await connection.initialize()

            listed = await connection.call({"action": "list_services"})
            assert listed["isError"] is False
            summary = next(s for s in make_result_payload(listed)["services"] if s["id"] == ref)
            assert summary["kind"] == "rest"
            assert summary["tool_count"] == 2
            assert summary["connected"] is True

            tools = await connection.call({"action": "list_tools", "service": ref})
            tool_ids = [tool["id"] for tool in make_result_payload(tools)["tools"]]
            assert f"{ref}/listItems" in tool_ids
            assert f"{ref}/getItem" in tool_ids

            invoked = await connection.call(
                {"action": "invoke", "id": f"{ref}/listItems", "args": {"q": "beta"}}
            )
            assert invoked["isError"] is False, invoked
            rows = json.loads(make_result_text(invoked))
            assert [row["name"] for row in rows] == ["beta"]
            seen = [r for r in mock_rest.requests if r.path == "/items"]
            assert seen and seen[-1].headers.get(API_KEY_NAME.lower()) == API_KEY_VALUE
    finally:
        await _remove_allowlist()


async def test_phase3_done_when_oauth_template(
    mock_rest: MockRestHandle, mock_oauth: MockOAuthHandle
) -> None:
    try:
        suffix = uuid.uuid4().hex[:10]
        engine = create_db_engine(Settings().database_url)
        factory = create_session_factory(engine)
        try:
            async with factory() as session:
                org = await ensure_org(session)
                try:
                    allowlist_row = await add_url_allowlist(
                        session, pattern="127.0.0.0/8", org_slug=org.slug
                    )
                    del allowlist_row
                except AllowlistError:
                    await session.rollback()
                alice = _user(org.id, suffix, "alice")
                bob = _user(org.id, suffix, "bob")
                session.add_all([alice, bob])
                await session.flush()
                raw_alice, _ = await create_token(
                    session, org_id=org.id, user_id=alice.id, label="dw3-alice"
                )
                raw_bob, _ = await create_token(
                    session, org_id=org.id, user_id=bob.id, label="dw3-bob"
                )
                from cam.registry.services import register_service

                slug = f"tpl{suffix}"
                service, _conn = await register_service(
                    session,
                    tier="template",
                    slug=slug,
                    name="Shared OAuth REST",
                    url=mock_rest.base_url,
                    kind="rest",
                    auth_type="oauth",
                    spec_url=mock_rest.spec_url,
                    description="Two-user OAuth template",
                )
                await session.commit()
                ref = f"global/{service.slug}"
                service_id = service.id
        finally:
            await engine.dispose()

        mock_rest.requests.clear()
        app = create_app()
        async with _running_app(app) as api:
            connect_path = f"/api/services/{ref}/connect"
            await _connect_template(api, raw_alice, connect_path, as_user="alice")
            await _connect_template(api, raw_bob, connect_path, as_user="bob")

            check_engine = create_db_engine(Settings().database_url)
            check_factory = create_session_factory(check_engine)
            try:
                async with check_factory() as session:
                    rows = (
                        (
                            await session.execute(
                                select(Connection).where(
                                    Connection.service_id == service_id,
                                    Connection.user_id.is_not(None),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    assert len(rows) == 2
                    assert rows[0].user_id != rows[1].user_id
                    alice_connection = next(
                        row for row in rows if row.secret_enc is not None and row.expires_at
                    )
                    assert alice_connection.status == "connected"
            finally:
                await check_engine.dispose()

            conn_alice = McpConnection(api, raw_alice)
            await conn_alice.initialize()
            listed = await conn_alice.call({"action": "list_services"})
            summary = next(s for s in make_result_payload(listed)["services"] if s["id"] == ref)
            assert summary["tier"] == "template"
            assert summary["connected"] is True

            mock_rest.requests.clear()
            invoked_alice = await conn_alice.call(
                {"action": "invoke", "id": f"{ref}/whoami", "args": {}}
            )
            assert invoked_alice["isError"] is False, invoked_alice
            who_alice = json.loads(make_result_text(invoked_alice))
            assert who_alice["auth"] == "bearer"

            conn_bob = McpConnection(api, raw_bob)
            await conn_bob.initialize()
            invoked_bob = await conn_bob.call(
                {"action": "invoke", "id": f"{ref}/whoami", "args": {}}
            )
            assert invoked_bob["isError"] is False, invoked_bob
            who_bob = json.loads(make_result_text(invoked_bob))
            assert who_bob["auth"] == "bearer"

            whoami_reqs = [r for r in mock_rest.requests if r.path == "/whoami"]
            assert len(whoami_reqs) == 2
            alice_auth = whoami_reqs[0].headers.get("authorization", "")
            bob_auth = whoami_reqs[1].headers.get("authorization", "")
            assert alice_auth.startswith("Bearer tok-alice-")
            assert bob_auth.startswith("Bearer tok-bob-")
            assert alice_auth != bob_auth
    finally:
        await _remove_allowlist()
