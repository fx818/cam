"""R17 schema support: `users.session_version` is a NOT NULL int defaulting to 0.

The dashboard session cookie carries a per-user version that is bumped to revoke
sessions (logout, password reset). This test pins the DB contract other modules
rely on: the column exists, defaults to 0, and round-trips through the ORM as
`user.session_version`.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cam.db.models import Org, User


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def test_users_table_has_session_version_column(db_session: AsyncSession) -> None:
    row = (
        await db_session.execute(
            text(
                "SELECT data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'session_version'"
            )
        )
    ).first()
    assert row is not None, "users.session_version column is missing"
    data_type, is_nullable, column_default = row
    assert data_type == "integer"
    assert is_nullable == "NO"
    assert column_default is not None and column_default.strip().startswith("0")


def test_user_model_exposes_session_version() -> None:
    assert "session_version" in User.__table__.columns


async def test_user_session_version_defaults_to_zero_and_increments(
    db_session: AsyncSession,
) -> None:
    org = Org(slug=_unique("org-sv"), name="Session Version Org")
    db_session.add(org)
    await db_session.flush()
    user = User(
        org_id=org.id,
        email=f"{_unique('sv')}@example.com",
        handle="sv-user",
        display_name="SV User",
        password_hash="x",
        role="user",
        kind="person",
    )
    db_session.add(user)
    await db_session.flush()
    assert user.session_version == 0

    user_id = user.id
    user.session_version = 1
    await db_session.commit()

    reloaded = await db_session.get(User, user_id)
    assert reloaded is not None
    assert reloaded.session_version == 1
