import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable

from click.testing import CliRunner
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.cli import main
from cam.connections.orgs import ensure_org
from cam.connections.passwords import verify_password
from cam.db.models import Org, User
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


def test_create_admin_team_kind() -> None:
    email = f"{_unique('Team.Member')}@example.com"
    result = CliRunner().invoke(
        main,
        [
            "create-admin",
            "--email",
            email,
            "--password",
            "adm-passw0rd-abc",
            "--kind",
            "team",
        ],
    )
    assert result.exit_code == 0, result.output
    user = asyncio.run(_get_user(email.lower()))
    assert user is not None
    assert user.role == "admin"
    assert user.kind == "team"
    assert user.handle == email.split("@")[0].lower()
    assert asyncio.run(verify_password("adm-passw0rd-abc", user.password_hash))


def test_create_admin_duplicate_email_rejected() -> None:
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


def test_create_admin_duplicate_handle_rejected() -> None:
    local = _unique("samehandle")
    runner = CliRunner()
    first = runner.invoke(
        main,
        [
            "create-admin",
            "--email",
            f"{local}@a.example.com",
            "--password",
            "pw-one-12345",
            "--handle",
            local,
        ],
    )
    assert first.exit_code == 0, first.output
    second = runner.invoke(
        main,
        [
            "create-admin",
            "--email",
            f"{local}@b.example.com",
            "--password",
            "pw-two-12345",
            "--handle",
            local,
        ],
    )
    assert second.exit_code == 1
    assert "handle" in second.output


def test_create_admin_empty_password_rejected() -> None:
    result = CliRunner().invoke(
        main,
        ["create-admin", "--email", f"{_unique('empty')}@example.com", "--password", ""],
    )
    assert result.exit_code == 1
    assert "password must not be empty" in result.output


def test_cli_bootstrap_creates_default_org_data_key() -> None:
    async def fn(session: AsyncSession) -> Org:
        org = await ensure_org(session)
        await session.commit()
        return org

    org = asyncio.run(_run(fn))
    assert org.slug == "indiamart"
    assert org.data_key_enc is not None


def test_retired_commands_are_gone() -> None:
    retired = [
        "create-user",
        "issue-token",
        "register-global-mcp",
        "register-personal-mcp",
        "set-service-status",
        "set-service-visibility",
        "grant-service-visibility",
        "revoke-service-visibility",
        "share-service",
        "unshare-service",
        "deactivate-user",
        "add-url-allowlist",
        "remove-url-allowlist",
    ]
    runner = CliRunner()
    for name in retired:
        result = runner.invoke(main, [name, "--help"])
        assert result.exit_code != 0, name
        assert "No such command" in result.output, name
