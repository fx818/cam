"""R25 schema support: composite tenant foreign keys.

A child row (`tools`, `connections`) must belong to the same org as its parent
service. The composite foreign keys make a cross-org child row impossible.
The `settings` table is intentionally left global (key/value, no `org_id`) per
architecture.md's Data model and the recorded coordinator decision; per-org
configuration lives in `orgs.settings`.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cam.db.base import Setting
from cam.db.models import Connection, Org, Service, Tool


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def _two_orgs_and_service(db_session: AsyncSession) -> tuple[uuid.UUID, Service]:
    org_a = Org(slug=_unique("org-a"), name="Org A")
    org_b = Org(slug=_unique("org-b"), name="Org B")
    db_session.add_all([org_a, org_b])
    await db_session.flush()
    service = Service(
        org_id=org_a.id,
        tier="global",
        slug=_unique("svc"),
        name="Service A",
        url="https://a.example/mcp",
    )
    db_session.add(service)
    await db_session.flush()
    return org_b.id, service


async def test_tool_org_must_match_service_org(db_session: AsyncSession) -> None:
    other_org_id, service = await _two_orgs_and_service(db_session)
    db_session.add(
        Tool(
            org_id=other_org_id,
            service_id=service.id,
            name="run",
            upstream_name="run",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_connection_org_must_match_service_org(db_session: AsyncSession) -> None:
    other_org_id, service = await _two_orgs_and_service(db_session)
    db_session.add(
        Connection(
            org_id=other_org_id,
            service_id=service.id,
            user_id=None,
            key_version=1,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


def test_settings_table_is_intentionally_global() -> None:
    # Deliberate: runtime settings stay global (architecture.md Data model);
    # per-org config lives in orgs.settings. Revisit only for multi-org SaaS.
    assert "org_id" not in Setting.__table__.columns
