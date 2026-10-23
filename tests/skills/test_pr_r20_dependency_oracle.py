"""R20: skill dependency resolution must not be an existence oracle.

A missing owner handle and a missing service slug must yield the *same*
opaque error, so the create-skill endpoint cannot be used to probe which
services or users exist in the org.
"""

from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.db.models import Org, Service, Skill, SkillRequires, User
from cam.skills import store
from cam.skills.validation import SkillValidationError, from_editor

_OPAQUE = re.compile(r"^'requires' entry '[^']+': no accessible service in this org$")


async def _org_and_owner(db: AsyncSession) -> tuple[Org, User]:
    org = Org(slug=f"r20-{uuid.uuid4().hex[:10]}", name="R20 Org")
    db.add(org)
    await db.flush()
    owner = User(
        org_id=org.id,
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        handle="owner",
        display_name="Owner",
        password_hash="x",
        role="user",
        kind="person",
    )
    db.add(owner)
    await db.flush()
    return org, owner


async def _create_with_requires(
    db: AsyncSession, org: Org, owner: User, requires: tuple[str, ...]
) -> tuple[Skill, set[uuid.UUID]]:
    source = from_editor(name="Sk", description="d", requires=list(requires), body="b")
    skill, _version = await store.create_skill(
        db,
        org_id=org.id,
        tier="personal",
        owner_user_id=owner.id,
        actor_user_id=owner.id,
        slug=f"sk-{uuid.uuid4().hex[:8]}",
        source=source,
        visibility="all",
        notes=None,
    )
    rows = (
        await db.execute(select(SkillRequires.service_id).where(SkillRequires.skill_id == skill.id))
    ).scalars()
    return skill, set(rows)


async def test_missing_owner_and_missing_service_are_opaque(db_session: AsyncSession) -> None:
    org, owner = await _org_and_owner(db_session)

    # A savepoint keeps the org/owner while discarding the half-created skill.
    with pytest.raises(SkillValidationError) as missing_owner:
        async with db_session.begin_nested():
            await _create_with_requires(db_session, org, owner, ("ghost-user/thing",))

    with pytest.raises(SkillValidationError) as missing_service:
        async with db_session.begin_nested():
            await _create_with_requires(db_session, org, owner, ("global/does-not-exist",))

    # Same opaque shape for both cases; no hint about owner vs service.
    assert _OPAQUE.match(str(missing_owner.value))
    assert _OPAQUE.match(str(missing_service.value))
    assert "no user" not in str(missing_owner.value)
    assert "no such service" not in str(missing_service.value)


async def test_existing_personal_service_resolves(db_session: AsyncSession) -> None:
    org, owner = await _org_and_owner(db_session)
    service = Service(
        org_id=org.id,
        tier="personal",
        owner_user_id=owner.id,
        slug="team-api",
        name="Team API",
        kind="mcp",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="none",
    )
    db_session.add(service)
    await db_session.flush()

    _skill, service_ids = await _create_with_requires(db_session, org, owner, ("owner/team-api",))
    assert service_ids == {service.id}


async def test_existing_global_service_resolves(db_session: AsyncSession) -> None:
    org, owner = await _org_and_owner(db_session)
    service = Service(
        org_id=org.id,
        tier="global",
        owner_user_id=None,
        slug="pns",
        name="PNS",
        kind="mcp",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="none",
    )
    db_session.add(service)
    await db_session.flush()

    _skill, service_ids = await _create_with_requires(db_session, org, owner, ("global/pns",))
    assert service_ids == {service.id}
