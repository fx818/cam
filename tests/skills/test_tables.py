import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cam.db.models import (
    Org,
    Service,
    Skill,
    SkillFile,
    SkillRequires,
    SkillVersion,
    User,
)


def _org(session: AsyncSession, suffix: str) -> Org:
    org = Org(slug=f"sk-org-{suffix}", name="Skill Org")
    session.add(org)
    return org


def _user(session: AsyncSession, org: Org, email: str, handle: str) -> User:
    user = User(
        org_id=org.id,
        email=email,
        handle=handle,
        display_name=handle.title(),
        password_hash="x",
        role="user",
        kind="person",
    )
    session.add(user)
    return user


@pytest.fixture
async def skill_org(db_session: AsyncSession) -> Org:
    org = _org(db_session, uuid.uuid4().hex[:10])
    await db_session.flush()
    return org


def _global_skill(org: Org, slug: str, owner: User | None = None) -> Skill:
    return Skill(
        org_id=org.id,
        tier="global" if owner is None else "personal",
        owner_user_id=owner.id if owner else None,
        slug=slug,
        name=slug.replace("-", " ").title(),
        description="desc",
    )


async def _setup(db_session: AsyncSession) -> tuple[Org, User]:
    suffix = uuid.uuid4().hex[:10]
    org = _org(db_session, suffix)
    await db_session.flush()
    owner = _user(db_session, org, f"{suffix}@example.com", handle=f"h{suffix}")
    await db_session.flush()
    return org, owner


async def test_skill_tables_exist(db_session: AsyncSession) -> None:
    org, owner = await _setup(db_session)
    skill = _global_skill(org, "analytics")
    db_session.add(skill)
    await db_session.flush()
    version = SkillVersion(
        org_id=org.id, skill_id=skill.id, version=1, created_by=owner.id, notes="first"
    )
    db_session.add(version)
    await db_session.flush()
    db_session.add(
        SkillFile(
            org_id=org.id,
            version_id=version.id,
            path="SKILL.md",
            content="---\nname: analytics\ndescription: d\n---\nbody",
        )
    )
    await db_session.flush()
    service = Service(
        org_id=org.id,
        tier="global",
        slug=f"dep-{uuid.uuid4().hex[:8]}",
        name="Dep",
        kind="mcp",
        url="https://example.com/mcp",
        auth_type="none",
    )
    db_session.add(service)
    await db_session.flush()
    db_session.add(SkillRequires(org_id=org.id, skill_id=skill.id, service_id=service.id))
    await db_session.flush()
    skill.current_version_id = version.id
    await db_session.flush()


async def test_global_slug_unique_per_org(db_session: AsyncSession) -> None:
    org, _owner = await _setup(db_session)
    db_session.add(_global_skill(org, "analytics"))
    await db_session.flush()
    db_session.add(_global_skill(org, "analytics"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_same_slug_in_two_orgs_ok(db_session: AsyncSession) -> None:
    org_a, _ = await _setup(db_session)
    org_b, _ = await _setup(db_session)
    db_session.add(_global_skill(org_a, "shared-slug"))
    db_session.add(_global_skill(org_b, "shared-slug"))
    await db_session.flush()


async def test_personal_slug_unique_within_owner(db_session: AsyncSession) -> None:
    org, owner = await _setup(db_session)
    _user(db_session, org, f"other-{org.slug}@example.com", f"other{org.slug}")
    await db_session.flush()
    db_session.add(_global_skill(org, "notes", owner=owner))
    db_session.add(_global_skill(org, "notes", owner=owner))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_same_personal_slug_for_two_owners_ok(db_session: AsyncSession) -> None:
    org, owner = await _setup(db_session)
    other = _user(db_session, org, f"other-{org.slug}@example.com", f"other{org.slug}")
    await db_session.flush()
    db_session.add(_global_skill(org, "notes", owner=owner))
    db_session.add(_global_skill(org, "notes", owner=other))
    await db_session.flush()


async def test_global_skill_must_not_have_owner(db_session: AsyncSession) -> None:
    org, owner = await _setup(db_session)
    skill = Skill(
        org_id=org.id,
        tier="global",
        owner_user_id=owner.id,
        slug="bad",
        name="Bad",
        description="d",
    )
    db_session.add(skill)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_personal_skill_must_have_owner(db_session: AsyncSession) -> None:
    org, _owner = await _setup(db_session)
    skill = Skill(org_id=org.id, tier="personal", slug="bad", name="Bad", description="d")
    db_session.add(skill)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_version_unique_per_skill(db_session: AsyncSession) -> None:
    org, owner = await _setup(db_session)
    skill = _global_skill(org, "versioned")
    db_session.add(skill)
    await db_session.flush()
    db_session.add(SkillVersion(org_id=org.id, skill_id=skill.id, version=1, created_by=owner.id))
    db_session.add(SkillVersion(org_id=org.id, skill_id=skill.id, version=1, created_by=owner.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_requires_row_needs_unique_service(db_session: AsyncSession) -> None:
    org, _ = await _setup(db_session)
    service = Service(
        org_id=org.id,
        tier="global",
        slug=f"dep-{org.slug}",
        name="Dep",
        kind="mcp",
        url="https://example.com/mcp",
        auth_type="none",
    )
    skill = _global_skill(org, "needdep")
    db_session.add_all([service, skill])
    await db_session.flush()
    db_session.add(SkillRequires(org_id=org.id, skill_id=skill.id, service_id=service.id))
    db_session.add(SkillRequires(org_id=org.id, skill_id=skill.id, service_id=service.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
