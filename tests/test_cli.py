import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable

from click.testing import CliRunner
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.cli import main
from cam.connections.orgs import decrypt_for_org, ensure_org, get_org_by_slug
from cam.connections.passwords import verify_password
from cam.connections.tokens import extract_token_prefix, hash_token
from cam.db.models import CamToken, Connection, Org, Service, User
from cam.db.session import create_db_engine, create_session_factory

TEST_DATABASE_URL = os.environ["CAM_DATABASE_URL"]


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _run[T](fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
    engine = create_db_engine(TEST_DATABASE_URL)
    try:
        async with create_session_factory(engine)() as session:
            return await fn(session)
    finally:
        await engine.dispose()


async def _get_user(email: str) -> User | None:
    async def fn(session: AsyncSession) -> User | None:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    return await _run(fn)


async def _get_service(slug: str) -> Service | None:
    async def fn(session: AsyncSession) -> Service | None:
        result = await session.execute(select(Service).where(Service.slug == slug))
        return result.scalar_one_or_none()

    return await _run(fn)


async def _get_connection(service_id: uuid.UUID) -> Connection | None:
    async def fn(session: AsyncSession) -> Connection | None:
        result = await session.execute(
            select(Connection).where(Connection.service_id == service_id)
        )
        return result.scalar_one_or_none()

    return await _run(fn)


async def _get_token(token_hash: str) -> CamToken | None:
    async def fn(session: AsyncSession) -> CamToken | None:
        result = await session.execute(select(CamToken).where(CamToken.token_hash == token_hash))
        return result.scalar_one_or_none()

    return await _run(fn)


async def _decrypt_connection_secret(connection_id: uuid.UUID) -> bytes:
    async def fn(session: AsyncSession) -> bytes:
        org = await get_org_by_slug(session, "indiamart")
        assert org is not None
        connection = await session.get(Connection, connection_id)
        assert connection is not None
        assert connection.secret_enc is not None
        result = await decrypt_for_org(session, org.id, connection.secret_enc)
        return result.plaintext

    return await _run(fn)


def test_create_admin_command() -> None:
    email = f"{_unique('admin')}@example.com"
    password = "adm1n-passw0rd-xyz"
    result = CliRunner().invoke(
        main,
        [
            "create-admin",
            "--email",
            email,
            "--password",
            password,
            "--display-name",
            "Test Admin",
        ],
    )
    assert result.exit_code == 0, result.output
    user = asyncio.run(_get_user(email))
    assert user is not None
    assert user.role == "admin"
    assert user.kind == "person"
    assert user.status == "active"
    assert user.email == email
    assert user.handle == email.split("@")[0]
    assert user.display_name == "Test Admin"
    assert user.password_hash.startswith("$argon2id$")
    assert password not in result.output
    assert asyncio.run(verify_password(password, user.password_hash)) is True
    assert asyncio.run(verify_password("wrong", user.password_hash)) is False
    assert f"(handle {user.handle})" in result.output


def test_create_user_command_with_team_kind() -> None:
    email = f"{_unique('Team.Member')}@example.com"
    result = CliRunner().invoke(
        main,
        [
            "create-user",
            "--email",
            email,
            "--password",
            "user-passw0rd-abc",
            "--kind",
            "team",
        ],
    )
    assert result.exit_code == 0, result.output
    user = asyncio.run(_get_user(email.lower()))
    assert user is not None
    assert user.role == "user"
    assert user.kind == "team"
    assert user.handle == email.split("@")[0].lower()
    assert asyncio.run(verify_password("user-passw0rd-abc", user.password_hash))


def test_create_user_duplicate_email_rejected() -> None:
    email = f"{_unique('dupemail')}@example.com"
    runner = CliRunner()
    first = runner.invoke(
        main,
        ["create-admin", "--email", email, "--password", "pw-one-12345"],
    )
    assert first.exit_code == 0, first.output
    second = runner.invoke(
        main,
        ["create-admin", "--email", email.upper(), "--password", "pw-two-12345"],
    )
    assert second.exit_code == 1
    assert "already registered" in second.output


def test_create_user_duplicate_handle_in_same_org_rejected() -> None:
    local = _unique("samehandle")
    runner = CliRunner()
    first = runner.invoke(
        main,
        ["create-user", "--email", f"{local}@a.example.com", "--password", "pw-one-12345"],
    )
    assert first.exit_code == 0, first.output
    second = runner.invoke(
        main,
        ["create-user", "--email", f"{local}@b.example.com", "--password", "pw-two-12345"],
    )
    assert second.exit_code == 1
    assert "handle" in second.output


def test_issue_token_prints_raw_token_once() -> None:
    email = f"{_unique('tokenholder')}@example.com"
    runner = CliRunner()
    created = runner.invoke(
        main,
        ["create-user", "--email", email, "--password", "pw-token-12345"],
    )
    assert created.exit_code == 0, created.output
    label = "laptop-codex"
    result = runner.invoke(main, ["issue-token", "--email", email, "--label", label])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.strip().splitlines() if line]
    assert len(lines) == 1
    raw = lines[0]
    assert raw.startswith("cam_")
    assert len(raw) == 47
    token = asyncio.run(_get_token(hash_token(raw)))
    assert token is not None
    assert token.label == label
    assert token.revoked_at is None
    assert token.token_prefix == extract_token_prefix(raw) == raw[4:12]
    assert raw not in token.token_hash
    assert raw not in token.token_prefix


def test_issue_token_unknown_email_fails() -> None:
    result = CliRunner().invoke(
        main,
        [
            "issue-token",
            "--email",
            f"{_unique('nobody')}@example.com",
            "--label",
            "x",
        ],
    )
    assert result.exit_code == 1
    assert "no user with email" in result.output


def test_register_global_mcp_static_header() -> None:
    slug = _unique("windm")
    header_value = f"secret-{uuid.uuid4().hex}"
    result = CliRunner().invoke(
        main,
        [
            "register-global-mcp",
            "--slug",
            slug,
            "--name",
            "Windmill",
            "--url",
            "https://windmill.example.com/api/mcp",
            "--transport",
            "streamable-http",
            "--auth-type",
            "static-header",
            "--header-name",
            "X-API-Key",
            "--header-value",
            header_value,
            "--description",
            "Automation platform",
        ],
    )
    assert result.exit_code == 0, result.output
    assert f"global/{slug}" in result.output
    assert header_value not in result.output
    service = asyncio.run(_get_service(slug))
    assert service is not None
    assert service.tier == "global"
    assert service.owner_user_id is None
    assert service.kind == "mcp"
    assert service.transport == "streamable-http"
    assert service.auth_type == "static-header"
    assert service.auth_config == {"header_name": "X-API-Key"}
    assert service.visibility == "all"
    assert service.status == "active"
    assert service.health == "unknown"
    assert service.timeout_ms == 60000
    assert service.response_cap_tokens == 50000
    connection = asyncio.run(_get_connection(service.id))
    assert connection is not None
    assert connection.user_id is None
    assert connection.status == "connected"
    assert connection.key_version == 1
    assert connection.secret_enc is not None
    assert header_value.encode() not in connection.secret_enc
    assert asyncio.run(_decrypt_connection_secret(connection.id)) == header_value.encode()


def test_register_global_mcp_auth_none() -> None:
    slug = _unique("openmcp")
    result = CliRunner().invoke(
        main,
        [
            "register-global-mcp",
            "--slug",
            slug,
            "--name",
            "Open MCP",
            "--url",
            "http://localhost:9999/mcp",
            "--transport",
            "sse",
            "--auth-type",
            "none",
        ],
    )
    assert result.exit_code == 0, result.output
    service = asyncio.run(_get_service(slug))
    assert service is not None
    assert service.auth_type == "none"
    assert service.auth_config == {}
    assert service.transport == "sse"
    connection = asyncio.run(_get_connection(service.id))
    assert connection is not None
    assert connection.user_id is None
    assert connection.secret_enc is None


def test_register_global_mcp_bearer() -> None:
    slug = _unique("grafana")
    token_value = f"bearer-{uuid.uuid4().hex}"
    result = CliRunner().invoke(
        main,
        [
            "register-global-mcp",
            "--slug",
            slug,
            "--name",
            "Grafana",
            "--url",
            "https://grafana.example.com/mcp",
            "--transport",
            "streamable-http",
            "--auth-type",
            "bearer",
            "--header-value",
            token_value,
        ],
    )
    assert result.exit_code == 0, result.output
    assert token_value not in result.output
    service = asyncio.run(_get_service(slug))
    assert service is not None
    assert service.auth_type == "bearer"
    connection = asyncio.run(_get_connection(service.id))
    assert connection is not None
    assert connection.secret_enc is not None
    assert asyncio.run(_decrypt_connection_secret(connection.id)) == token_value.encode()


def test_register_global_mcp_duplicate_slug_rejected() -> None:
    slug = _unique("dupsvc")
    runner = CliRunner()
    args = [
        "register-global-mcp",
        "--slug",
        slug,
        "--name",
        "One",
        "--url",
        "https://one.example.com/mcp",
        "--transport",
        "streamable-http",
        "--auth-type",
        "none",
    ]
    first = runner.invoke(main, args)
    assert first.exit_code == 0, first.output
    second = runner.invoke(main, args)
    assert second.exit_code == 1
    assert "already registered" in second.output


def test_register_global_mcp_static_header_requires_value() -> None:
    result = CliRunner().invoke(
        main,
        [
            "register-global-mcp",
            "--slug",
            _unique("needval"),
            "--name",
            "Needs Value",
            "--url",
            "https://needs.example.com/mcp",
            "--transport",
            "streamable-http",
            "--auth-type",
            "static-header",
            "--header-name",
            "X-Key",
        ],
        input="\n",
    )
    assert result.exit_code == 1


def test_register_global_mcp_rejects_bad_slug() -> None:
    result = CliRunner().invoke(
        main,
        [
            "register-global-mcp",
            "--slug",
            "Bad Slug!",
            "--name",
            "Bad",
            "--url",
            "https://bad.example.com/mcp",
            "--transport",
            "streamable-http",
            "--auth-type",
            "none",
        ],
    )
    assert result.exit_code == 1
    assert "slug" in result.output


def test_cli_bootstrap_creates_default_org_data_key() -> None:
    async def fn(session: AsyncSession) -> Org:
        org = await ensure_org(session)
        await session.commit()
        return org

    org = asyncio.run(_run(fn))
    assert org.slug == "indiamart"
    assert org.data_key_enc is not None


def test_register_personal_mcp_and_status_cli_commands() -> None:
    email = f"{_unique('persowner')}@example.com"
    runner = CliRunner()
    created = runner.invoke(main, ["create-user", "--email", email, "--password", "pw-pers-12345"])
    assert created.exit_code == 0, created.output
    handle = email.split("@")[0].lower()
    slug = _unique("teamapi")

    registered = runner.invoke(
        main,
        [
            "register-personal-mcp",
            "--email",
            email,
            "--slug",
            slug,
            "--name",
            "Team API",
            "--url",
            "https://teamapi.example.com/mcp",
            "--transport",
            "streamable-http",
            "--auth-type",
            "bearer",
            "--header-value",
            "personal-secret-value",
        ],
    )
    assert registered.exit_code == 0, registered.output
    assert f"{handle}/{slug}" in registered.output
    assert "personal-secret-value" not in registered.output

    service = asyncio.run(_get_service(slug))
    assert service is not None
    assert service.tier == "personal"
    assert service.status == "active"
    connection = asyncio.run(_get_connection(service.id))
    assert connection is not None
    assert connection.user_id is not None
    assert asyncio.run(_decrypt_connection_secret(connection.id)) == b"personal-secret-value"

    disabled = runner.invoke(
        main,
        ["set-service-status", "--service", f"{handle}/{slug}", "--status", "disabled"],
    )
    assert disabled.exit_code == 0, disabled.output
    assert "disabled" in disabled.output
    service = asyncio.run(_get_service(slug))
    assert service is not None
    assert service.status == "disabled"

    enabled = runner.invoke(
        main,
        ["set-service-status", "--service", f"{handle}/{slug}", "--status", "active"],
    )
    assert enabled.exit_code == 0, enabled.output
    service = asyncio.run(_get_service(slug))
    assert service is not None
    assert service.status == "active"

    duplicate = runner.invoke(
        main,
        [
            "register-personal-mcp",
            "--email",
            email,
            "--slug",
            slug,
            "--name",
            "Team API Again",
            "--url",
            "https://teamapi.example.com/mcp",
            "--transport",
            "streamable-http",
            "--auth-type",
            "none",
        ],
    )
    assert duplicate.exit_code == 1
    assert "already registered" in duplicate.output


def test_share_and_unshare_service_cli_commands() -> None:
    owner_email = f"{_unique('shareowner')}@example.com"
    grantee_email = f"{_unique('sharegrantee')}@example.com"
    runner = CliRunner()
    assert (
        runner.invoke(
            main, ["create-user", "--email", owner_email, "--password", "pw-own-12345"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            main, ["create-user", "--email", grantee_email, "--password", "pw-gr-12345"]
        ).exit_code
        == 0
    )
    owner_handle = owner_email.split("@")[0].lower()
    slug = _unique("sharedsvc")
    registered = runner.invoke(
        main,
        [
            "register-personal-mcp",
            "--email",
            owner_email,
            "--slug",
            slug,
            "--name",
            "Shareable",
            "--url",
            "https://shareable.example.com/mcp",
            "--transport",
            "streamable-http",
            "--auth-type",
            "none",
        ],
    )
    assert registered.exit_code == 0, registered.output
    ref = f"{owner_handle}/{slug}"

    shared = runner.invoke(
        main,
        ["share-service", "--service", ref, "--owner", owner_email, "--user", grantee_email],
    )
    assert shared.exit_code == 0, shared.output
    assert grantee_email in shared.output

    duplicate = runner.invoke(
        main,
        ["share-service", "--service", ref, "--owner", owner_email, "--user", grantee_email],
    )
    assert duplicate.exit_code == 1
    assert "already shared" in duplicate.output

    revoked = runner.invoke(
        main,
        ["unshare-service", "--service", ref, "--owner", owner_email, "--user", grantee_email],
    )
    assert revoked.exit_code == 0, revoked.output

    again = runner.invoke(
        main,
        ["unshare-service", "--service", ref, "--owner", owner_email, "--user", grantee_email],
    )
    assert again.exit_code == 1
    assert "not shared" in again.output


def test_register_personal_mcp_unknown_email_fails() -> None:
    result = CliRunner().invoke(
        main,
        [
            "register-personal-mcp",
            "--email",
            f"{_unique('ghost')}@example.com",
            "--slug",
            _unique("ghostroute"),
            "--name",
            "Ghost",
            "--url",
            "https://ghost.example.com/mcp",
            "--transport",
            "streamable-http",
            "--auth-type",
            "none",
        ],
    )
    assert result.exit_code == 1
    assert "no user with email" in result.output


def test_service_visibility_cli_commands() -> None:
    slug = _unique("viscli")
    runner = CliRunner()
    registered = runner.invoke(
        main,
        [
            "register-global-mcp",
            "--slug",
            slug,
            "--name",
            "Visibility CLI",
            "--url",
            "https://viscli.example.com/mcp",
            "--transport",
            "streamable-http",
            "--auth-type",
            "none",
        ],
    )
    assert registered.exit_code == 0, registered.output

    email = f"{_unique('visuser')}@example.com"
    created = runner.invoke(main, ["create-user", "--email", email, "--password", "pw-vis-12345"])
    assert created.exit_code == 0, created.output

    restricted = runner.invoke(
        main,
        ["set-service-visibility", "--service", slug, "--visibility", "restricted"],
    )
    assert restricted.exit_code == 0, restricted.output
    assert "restricted" in restricted.output

    granted = runner.invoke(
        main,
        ["grant-service-visibility", "--service", slug, "--user", email],
    )
    assert granted.exit_code == 0, granted.output
    assert email in granted.output

    duplicate = runner.invoke(
        main,
        ["grant-service-visibility", "--service", slug, "--user", email],
    )
    assert duplicate.exit_code == 1
    assert "already has visibility" in duplicate.output

    revoked = runner.invoke(
        main,
        ["revoke-service-visibility", "--service", slug, "--user", email],
    )
    assert revoked.exit_code == 0, revoked.output

    missing = runner.invoke(
        main,
        ["revoke-service-visibility", "--service", slug, "--user", email],
    )
    assert missing.exit_code == 1
    assert "no visibility grant" in missing.output

    unknown_service = runner.invoke(
        main,
        [
            "set-service-visibility",
            "--service",
            _unique("ghostsvc"),
            "--visibility",
            "all",
        ],
    )
    assert unknown_service.exit_code == 1
    assert "no global or template service" in unknown_service.output
