import asyncio
import os
import uuid

import alembic.command
import alembic.config
import pytest
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from cam.db.models import Connection as ConnectionRow
from cam.db.models import Org, Service, Tool, User
from cam.db.session import create_db_engine

TEST_DATABASE_URL = os.environ["CAM_DATABASE_URL"]

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_TEST_URL = "postgresql+asyncpg://cam:cam@localhost:5432/cam_migration_test"

PHASE1_TABLES = (
    "orgs",
    "users",
    "cam_tokens",
    "services",
    "tools",
    "connections",
    "audit_logs",
    "url_allowlist",
    "login_attempts",
)


def _unique(suffix: str) -> str:
    return f"{suffix}-{uuid.uuid4().hex[:12]}"


def _column_names(sync_conn: Connection, table: str) -> set[str]:
    return {col["name"] for col in inspect(sync_conn).get_columns(table)}


async def test_phase1_tables_exist() -> None:
    engine = create_db_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
        assert set(PHASE1_TABLES) <= tables
    finally:
        await engine.dispose()


async def test_phase1_tables_carry_org_id() -> None:
    engine = create_db_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            for table in PHASE1_TABLES:
                if table == "orgs":
                    continue
                columns = await conn.run_sync(_column_names, table)
                assert "org_id" in columns, f"{table} is missing org_id"
    finally:
        await engine.dispose()


async def test_default_org_seeded() -> None:
    engine = create_db_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT slug, name, settings, data_key_enc FROM orgs WHERE slug = :s"),
                    {"s": "indiamart"},
                )
            ).first()
        assert row is not None
        assert row.slug == "indiamart"
        assert row.name == "IndiaMART"
        assert row.settings == {}
        assert row.data_key_enc is None
    finally:
        await engine.dispose()


async def test_email_unique_globally(db_session: AsyncSession) -> None:
    org_a = Org(slug=_unique("org-a"), name="Org A")
    org_b = Org(slug=_unique("org-b"), name="Org B")
    db_session.add_all([org_a, org_b])
    await db_session.flush()
    email = f"{uuid.uuid4().hex[:12]}@example.com"
    db_session.add(
        User(
            org_id=org_a.id,
            email=email,
            handle="alice",
            display_name="Alice",
            password_hash="x",
            role="user",
            kind="person",
        )
    )
    await db_session.flush()
    db_session.add(
        User(
            org_id=org_b.id,
            email=email,
            handle="bob",
            display_name="Bob",
            password_hash="x",
            role="user",
            kind="person",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_handle_unique_per_org(db_session: AsyncSession) -> None:
    org_a = Org(slug=_unique("org-a"), name="Org A")
    org_b = Org(slug=_unique("org-b"), name="Org B")
    db_session.add_all([org_a, org_b])
    await db_session.flush()
    db_session.add_all(
        [
            User(
                org_id=org_a.id,
                email=_unique("one") + "@example.com",
                handle="shared",
                display_name="One",
                password_hash="x",
                role="user",
                kind="person",
            ),
            User(
                org_id=org_b.id,
                email=_unique("two") + "@example.com",
                handle="shared",
                display_name="Two",
                password_hash="x",
                role="user",
                kind="person",
            ),
        ]
    )
    await db_session.flush()
    db_session.add(
        User(
            org_id=org_a.id,
            email=_unique("three") + "@example.com",
            handle="shared",
            display_name="Three",
            password_hash="x",
            role="user",
            kind="person",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_global_service_slug_unique_per_org(db_session: AsyncSession) -> None:
    org_a = Org(slug=_unique("org-a"), name="Org A")
    org_b = Org(slug=_unique("org-b"), name="Org B")
    db_session.add_all([org_a, org_b])
    await db_session.flush()
    slug = _unique("svc")
    db_session.add(
        Service(org_id=org_a.id, tier="global", slug=slug, name="One", url="https://a.example/mcp")
    )
    await db_session.flush()
    db_session.add(
        Service(
            org_id=org_b.id, tier="global", slug=slug, name="Other", url="https://b.example/mcp"
        )
    )
    await db_session.flush()
    db_session.add(
        Service(org_id=org_a.id, tier="global", slug=slug, name="Dup", url="https://c.example/mcp")
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_personal_service_slugs_unique_per_owner(db_session: AsyncSession) -> None:
    org = Org(slug=_unique("org-p"), name="Org P")
    db_session.add(org)
    await db_session.flush()
    owner_a = User(
        org_id=org.id,
        email=_unique("a") + "@example.com",
        handle="owner-a",
        display_name="A",
        password_hash="x",
        role="user",
        kind="person",
    )
    owner_b = User(
        org_id=org.id,
        email=_unique("b") + "@example.com",
        handle="owner-b",
        display_name="B",
        password_hash="x",
        role="user",
        kind="person",
    )
    db_session.add_all([owner_a, owner_b])
    await db_session.flush()
    slug = _unique("personal")
    db_session.add(
        Service(
            org_id=org.id,
            tier="personal",
            owner_user_id=owner_a.id,
            slug=slug,
            name="A personal",
            url="https://a.example/mcp",
        )
    )
    await db_session.flush()
    db_session.add(
        Service(
            org_id=org.id,
            tier="personal",
            owner_user_id=owner_b.id,
            slug=slug,
            name="B personal",
            url="https://b.example/mcp",
        )
    )
    await db_session.flush()
    db_session.add(
        Service(
            org_id=org.id,
            tier="personal",
            owner_user_id=owner_a.id,
            slug=slug,
            name="Dup",
            url="https://c.example/mcp",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_service_owner_enforced_by_tier(db_session: AsyncSession) -> None:
    org = Org(slug=_unique("org-t"), name="Org T")
    db_session.add(org)
    await db_session.commit()
    org_id = org.id
    db_session.add(
        Service(org_id=org_id, tier="personal", slug=_unique("p"), name="P", url="https://a/mcp")
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
    owner = User(
        org_id=org_id,
        email=_unique("o") + "@example.com",
        handle="owner",
        display_name="O",
        password_hash="x",
        role="admin",
        kind="person",
    )
    db_session.add(owner)
    await db_session.flush()
    db_session.add(
        Service(
            org_id=org_id,
            tier="global",
            owner_user_id=owner.id,
            slug=_unique("g"),
            name="G",
            url="https://b/mcp",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_connection_uniqueness(db_session: AsyncSession) -> None:
    org = Org(slug=_unique("org-c"), name="Org C")
    db_session.add(org)
    await db_session.flush()
    service = Service(
        org_id=org.id, tier="global", slug=_unique("svc"), name="S", url="https://a/mcp"
    )
    user = User(
        org_id=org.id,
        email=_unique("u") + "@example.com",
        handle="user",
        display_name="U",
        password_hash="x",
        role="user",
        kind="person",
    )
    db_session.add_all([service, user])
    await db_session.flush()
    org_id = org.id
    service_id = service.id
    user_id = user.id
    db_session.add(ConnectionRow(org_id=org_id, service_id=service_id, user_id=None, key_version=1))
    await db_session.flush()
    db_session.add(
        ConnectionRow(org_id=org_id, service_id=service_id, user_id=user_id, key_version=1)
    )
    await db_session.commit()
    db_session.add(ConnectionRow(org_id=org_id, service_id=service_id, user_id=None, key_version=1))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
    db_session.add(
        ConnectionRow(org_id=org_id, service_id=service_id, user_id=user_id, key_version=1)
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_tool_unique_per_service(db_session: AsyncSession) -> None:
    org = Org(slug=_unique("org-tool"), name="Org Tool")
    db_session.add(org)
    await db_session.flush()
    svc_a = Service(
        org_id=org.id, tier="global", slug=_unique("svc-a"), name="A", url="https://a/mcp"
    )
    svc_b = Service(
        org_id=org.id, tier="global", slug=_unique("svc-b"), name="B", url="https://b/mcp"
    )
    db_session.add_all([svc_a, svc_b])
    await db_session.flush()
    db_session.add(Tool(org_id=org.id, service_id=svc_a.id, name="run", upstream_name="run"))
    await db_session.flush()
    db_session.add(Tool(org_id=org.id, service_id=svc_b.id, name="run", upstream_name="run"))
    await db_session.flush()
    db_session.add(Tool(org_id=org.id, service_id=svc_a.id, name="run", upstream_name="run"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_audit_log_indexes() -> None:
    engine = create_db_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'audit_logs'")
            )
            names = {row[0] for row in rows}
        assert {
            "ix_audit_logs_org_id_user_id_ts",
            "ix_audit_logs_org_id_owner_user_id_ts",
            "ix_audit_logs_org_id_service_id_ts",
            "ix_audit_logs_ts",
        } <= names
    finally:
        await engine.dispose()


def _database_name(url: str) -> tuple[str, str]:
    base, _, name = url.rpartition("/")
    return base, name


async def _ensure_database(url: str) -> None:
    base, name = _database_name(url)
    engine = create_async_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            )
            if result.first() is None:
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


def test_migrations_apply_and_reverse_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_ensure_database(MIGRATION_TEST_URL))
    monkeypatch.setenv("CAM_DATABASE_URL", MIGRATION_TEST_URL)
    cfg = alembic.config.Config(os.path.join(REPO_ROOT, "alembic.ini"))
    alembic.command.upgrade(cfg, "head")
    assert set(PHASE1_TABLES) <= asyncio.run(_list_tables(MIGRATION_TEST_URL))
    assert asyncio.run(_indiamart_count(MIGRATION_TEST_URL)) == 1
    alembic.command.downgrade(cfg, "base")
    downgraded = asyncio.run(_list_tables(MIGRATION_TEST_URL))
    assert "orgs" not in downgraded and "settings" not in downgraded
    alembic.command.upgrade(cfg, "head")
    assert set(PHASE1_TABLES) <= asyncio.run(_list_tables(MIGRATION_TEST_URL))
    assert asyncio.run(_indiamart_count(MIGRATION_TEST_URL)) == 1


async def _list_tables(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
    finally:
        await engine.dispose()


async def _indiamart_count(url: str) -> int:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT count(*) FROM orgs WHERE slug = 'indiamart'"))
            return int(result.scalar_one())
    finally:
        await engine.dispose()
