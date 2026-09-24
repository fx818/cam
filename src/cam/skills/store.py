from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.db.models import (
    Service,
    Skill,
    SkillFile,
    SkillRequires,
    SkillShare,
    SkillVersion,
    SkillVisibility,
    User,
)
from cam.skills.validation import SkillSourceFiles, SkillValidationError


class SkillAlreadyExists(SkillValidationError):
    pass


async def check_slug_available(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    tier: str,
    owner_user_id: uuid.UUID | None,
    slug: str,
) -> None:
    if tier == "global":
        result = await session.execute(
            select(Skill.id).where(
                Skill.org_id == org_id, Skill.tier == "global", Skill.slug == slug
            )
        )
    else:
        result = await session.execute(
            select(Skill.id).where(
                Skill.org_id == org_id,
                Skill.tier == "personal",
                Skill.owner_user_id == owner_user_id,
                Skill.slug == slug,
            )
        )
    if result.first() is not None:
        raise SkillAlreadyExists(f"a skill with slug {slug!r} already exists here")


async def _resolve_requires(
    session: AsyncSession, org_id: uuid.UUID, refs: tuple[str, ...]
) -> dict[str, Service]:
    from cam.skills.validation import parse_required_ref

    normalized: list[str] = []
    for raw in refs:
        owner_part, slug = parse_required_ref(raw)
        normalized.append(f"{owner_part}/{slug}")
    owners: set[str] = {ref.split("/", 1)[0] for ref in normalized}
    user_ids: dict[str, uuid.UUID] = {}
    for owner_part in owners:
        if owner_part == "global":
            continue
        row = await session.execute(
            select(User.id).where(User.org_id == org_id, User.handle == owner_part)
        )
        found = row.scalar_one_or_none()
        if found is None:
            raise SkillValidationError(
                f"'requires' entry {owner_part}/…: no user with handle {owner_part!r} in this org"
            )
        user_ids[owner_part] = found
    resolved: dict[str, Service] = {}
    for ref in normalized:
        owner_part, slug = ref.split("/", 1)
        if owner_part == "global":
            result = await session.execute(
                select(Service).where(
                    Service.org_id == org_id,
                    Service.tier.in_(("global", "template")),
                    Service.slug == slug,
                )
            )
        else:
            result = await session.execute(
                select(Service).where(
                    Service.org_id == org_id,
                    Service.tier == "personal",
                    Service.owner_user_id == user_ids[owner_part],
                    Service.slug == slug,
                )
            )
        service = result.scalar_one_or_none()
        if service is None:
            raise SkillValidationError(f"'requires' entry {ref!r}: no such service in this org")
        resolved[ref] = service
    return resolved


async def _next_version_number(session: AsyncSession, skill_id: uuid.UUID) -> int:
    highest = await session.scalar(
        select(func.max(SkillVersion.version)).where(SkillVersion.skill_id == skill_id)
    )
    return int(highest or 0) + 1


async def _persist_version(
    session: AsyncSession,
    *,
    skill: Skill,
    source: SkillSourceFiles,
    version_number: int,
    actor_user_id: uuid.UUID | None,
    notes: str | None,
) -> SkillVersion:
    version = SkillVersion(
        org_id=skill.org_id,
        skill_id=skill.id,
        version=version_number,
        created_by=actor_user_id,
        notes=notes or None,
    )
    session.add(version)
    await session.flush()
    session.add_all(
        SkillFile(org_id=skill.org_id, version_id=version.id, path=path, content=content)
        for path, content in sorted(source.files.items())
    )
    await session.flush()
    skill.name = source.name
    skill.description = source.description
    skill.current_version_id = version.id
    await session.flush()
    return version


async def _persist_requires(
    session: AsyncSession, *, skill: Skill, source: SkillSourceFiles
) -> None:
    await session.execute(delete(SkillRequires).where(SkillRequires.skill_id == skill.id))
    resolved = await _resolve_requires(session, skill.org_id, source.requires)
    session.add_all(
        SkillRequires(org_id=skill.org_id, skill_id=skill.id, service_id=service.id)
        for _ref, service in resolved.items()
    )


async def create_skill(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    tier: str,
    owner_user_id: uuid.UUID | None,
    actor_user_id: uuid.UUID,
    slug: str,
    source: SkillSourceFiles,
    visibility: str,
    notes: str | None,
) -> tuple[Skill, SkillVersion]:
    await check_slug_available(
        session, org_id=org_id, tier=tier, owner_user_id=owner_user_id, slug=slug
    )
    skill = Skill(
        org_id=org_id,
        tier=tier,
        owner_user_id=owner_user_id,
        slug=slug,
        name=source.name,
        description=source.description,
        visibility=visibility,
    )
    session.add(skill)
    await session.flush()
    version = await _persist_version(
        session,
        skill=skill,
        source=source,
        version_number=1,
        actor_user_id=actor_user_id,
        notes=notes,
    )
    await _persist_requires(session, skill=skill, source=source)
    await session.flush()
    return skill, version


async def add_version(
    session: AsyncSession,
    skill: Skill,
    *,
    source: SkillSourceFiles,
    actor_user_id: uuid.UUID,
    notes: str | None,
) -> SkillVersion:
    version = await _persist_version(
        session,
        skill=skill,
        source=source,
        version_number=await _next_version_number(session, skill.id),
        actor_user_id=actor_user_id,
        notes=notes,
    )
    await _persist_requires(session, skill=skill, source=source)
    await session.flush()
    return version


async def rollback_to_version(
    session: AsyncSession,
    skill: Skill,
    *,
    target: SkillVersion,
    actor_user_id: uuid.UUID,
    notes: str | None,
) -> SkillVersion:
    from cam.skills.validation import parse_bundle

    files = await version_files(session, target.id)
    source = parse_bundle({path: content.encode() for path, content in files.items()})
    return await add_version(
        session,
        skill,
        source=source,
        actor_user_id=actor_user_id,
        notes=notes or f"rollback to version {target.version}",
    )


async def list_versions(session: AsyncSession, skill: Skill) -> list[SkillVersion]:
    rows = await session.execute(
        select(SkillVersion)
        .where(SkillVersion.skill_id == skill.id)
        .order_by(SkillVersion.version.desc())
    )
    return list(rows.scalars())


async def version_files(session: AsyncSession, version_id: uuid.UUID) -> dict[str, str]:
    rows = await session.execute(
        select(SkillFile.path, SkillFile.content)
        .where(SkillFile.version_id == version_id)
        .order_by(SkillFile.path)
    )
    return {path: content for path, content in rows.all()}


async def current_version(session: AsyncSession, skill: Skill) -> SkillVersion | None:
    if skill.current_version_id is None:
        return None
    return await session.get(SkillVersion, skill.current_version_id)


async def delete_skill(session: AsyncSession, skill: Skill) -> None:
    version_rows = await session.execute(
        select(SkillVersion.id).where(SkillVersion.skill_id == skill.id)
    )
    version_ids = [row[0] for row in version_rows]
    if version_ids:
        await session.execute(delete(SkillFile).where(SkillFile.version_id.in_(version_ids)))
    await session.execute(delete(SkillVersion).where(SkillVersion.skill_id == skill.id))
    await session.execute(delete(SkillRequires).where(SkillRequires.skill_id == skill.id))
    await session.execute(delete(SkillShare).where(SkillShare.skill_id == skill.id))
    await session.execute(delete(SkillVisibility).where(SkillVisibility.skill_id == skill.id))
    await session.flush()
    await session.delete(skill)
    await session.flush()
