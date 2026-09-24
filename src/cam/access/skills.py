from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.access.resolution import GLOBAL_OWNER, has_access
from cam.connections.tokens import AuthenticatedIdentity
from cam.db.models import (
    CamToken,
    Service,
    Skill,
    SkillRequires,
    SkillShare,
    SkillVersion,
    SkillVisibility,
    User,
)

GLOBAL_SKILL_OWNER = GLOBAL_OWNER

OWNER_DEACTIVATED_REASON = "its owner's account is deactivated"


class SkillNotFound(Exception):
    """Unknown skill ref or no visibility for the caller."""


class SkillUnavailable(Exception):
    """The caller can see the skill, but its requirements are unmet."""

    def __init__(self, reason: str, skill_version_id: uuid.UUID | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.skill_version_id = skill_version_id


class SkillShareError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SkillSummary:
    skill_id: str
    id: str
    tier: str
    slug: str
    name: str
    description: str
    author: str | None
    owner_handle: str | None
    requires: tuple[str, ...]
    version: int | None
    available: bool
    unavailable_reason: str | None


@dataclass(frozen=True, slots=True)
class ResolvedSkill:
    skill: Skill
    ref: str
    owner_handle: str | None
    required_refs: tuple[str, ...]
    available: bool
    unavailable_reason: str | None


def parse_skill_ref(raw: str) -> tuple[str, str]:
    parts = raw.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise SkillNotFound(raw)
    owner_part = parts[0].strip().lower()
    slug = parts[1].strip()
    if not owner_part or not slug:
        raise SkillNotFound(raw)
    return owner_part, slug


def skill_ref(skill: Skill, owner_handle: str | None) -> str:
    if skill.tier == "global":
        return f"{GLOBAL_OWNER}/{skill.slug}"
    if not owner_handle:
        raise ValueError(f"owner handle required to build the ref for skill {skill.slug!r}")
    return f"{owner_handle}/{skill.slug}"


def _viewer_identity(org_id: uuid.UUID, user: User) -> AuthenticatedIdentity:
    return AuthenticatedIdentity(
        org_id=org_id,
        user=user,
        token=CamToken(
            org_id=org_id,
            user_id=user.id,
            label="dashboard",
            token_hash="dashboard",
            token_prefix="dashboard",
        ),
    )


async def _lookup_skill(
    session: AsyncSession, org_id: uuid.UUID, owner_part: str, slug: str
) -> tuple[Skill, str | None]:
    if owner_part == GLOBAL_SKILL_OWNER:
        result = await session.execute(
            select(Skill).where(Skill.org_id == org_id, Skill.tier == "global", Skill.slug == slug)
        )
        skill = result.scalar_one_or_none()
        if skill is None:
            raise SkillNotFound(f"{GLOBAL_SKILL_OWNER}/{slug}")
        return skill, None
    owner_result = await session.execute(
        select(User).where(User.org_id == org_id, User.handle == owner_part)
    )
    owner_user = owner_result.scalar_one_or_none()
    if owner_user is None:
        raise SkillNotFound(f"{owner_part}/{slug}")
    result = await session.execute(
        select(Skill).where(
            Skill.org_id == org_id,
            Skill.tier == "personal",
            Skill.slug == slug,
            Skill.owner_user_id == owner_user.id,
        )
    )
    skill = result.scalar_one_or_none()
    if skill is None:
        raise SkillNotFound(f"{owner_part}/{slug}")
    return skill, owner_user.handle


async def skill_access_state(
    session: AsyncSession, org_id: uuid.UUID, user: User, skill: Skill
) -> Literal["none", "granted", "owner_deactivated"]:
    if skill.org_id != org_id:
        return "none"
    if skill.tier == "personal":
        if skill.owner_user_id == user.id:
            return "granted"
        result = await session.execute(
            select(SkillShare.id).where(
                SkillShare.org_id == org_id,
                SkillShare.skill_id == skill.id,
                SkillShare.grantee_user_id == user.id,
                SkillShare.revoked_at.is_(None),
            )
        )
        if result.first() is None:
            return "none"
        owner = await session.get(User, skill.owner_user_id)
        if owner is None or owner.status != "active" or owner.deactivated_at is not None:
            return "owner_deactivated"
        return "granted"
    if skill.visibility == "all":
        return "granted"
    result = await session.execute(
        select(SkillVisibility.id).where(
            SkillVisibility.org_id == org_id,
            SkillVisibility.skill_id == skill.id,
            SkillVisibility.user_id == user.id,
        )
    )
    return "granted" if result.first() is not None else "none"


async def required_services(session: AsyncSession, skill: Skill) -> dict[str, Service]:
    rows = (
        await session.execute(
            select(Service, User)
            .join(SkillRequires, SkillRequires.service_id == Service.id)
            .outerjoin(User, User.id == Service.owner_user_id)
            .where(SkillRequires.skill_id == skill.id, SkillRequires.org_id == skill.org_id)
        )
    ).all()
    required: dict[str, Service] = {}
    for service, owner in rows:
        if service.tier == "personal" and owner is not None:
            ref = f"{owner.handle.lower()}/{service.slug}"
        else:
            ref = f"{GLOBAL_OWNER}/{service.slug}"
        required[ref] = service
    return dict(sorted(required.items()))


def compose_unavailable_text(missing: list[str] | tuple[str, ...]) -> str:
    return f"requires {', '.join(sorted(missing))} (no access)"


async def analyze_requirements(
    session: AsyncSession, org_id: uuid.UUID, user: User, skill: Skill
) -> tuple[bool, str | None, tuple[str, ...]]:
    required = await required_services(session, skill)
    missing: list[str] = []
    for ref, service in required.items():
        if not await has_access(session, _viewer_identity(org_id, user), service):
            missing.append(ref)
    if missing:
        return False, compose_unavailable_text(missing), tuple(sorted(required))
    return True, None, tuple(sorted(required))


async def list_skills(session: AsyncSession, org_id: uuid.UUID, user: User) -> list[SkillSummary]:
    rows = (
        await session.execute(
            select(Skill, User)
            .outerjoin(User, User.id == Skill.owner_user_id)
            .where(Skill.org_id == org_id)
            .order_by(Skill.slug, Skill.id)
        )
    ).all()
    visible: list[tuple[Skill, User | None]] = []
    for skill, owner_row in rows:
        state = await skill_access_state(session, org_id, user, skill)
        if state == "granted":
            visible.append((skill, owner_row))
        elif state == "owner_deactivated":
            if owner_row is None:
                owner_row = await session.get(User, skill.owner_user_id)
            visible.append((skill, owner_row))

    version_ids = {
        skill.current_version_id for skill, _owner in visible if skill.current_version_id
    }
    versions: dict[uuid.UUID, SkillVersion] = {}
    if version_ids:
        version_rows = await session.execute(
            select(SkillVersion).where(SkillVersion.id.in_(version_ids))
        )
        versions = {version.id: version for version in version_rows.scalars()}

    summaries: list[SkillSummary] = []
    for skill, owner_row in visible:
        available, reason, requires = await analyze_requirements(session, org_id, user, skill)
        summary_available = available
        summary_reason = reason
        author: str | None = owner_row.display_name if owner_row else None
        if (
            skill.tier == "personal"
            and user.id != skill.owner_user_id
            and owner_row is not None
            and (owner_row.status != "active" or owner_row.deactivated_at is not None)
        ):
            summary_available = False
            summary_reason = OWNER_DEACTIVATED_REASON
            requires = ()

        current_version_number: int | None = None
        current = versions.get(skill.current_version_id) if skill.current_version_id else None
        if current is not None:
            current_version_number = current.version

        summaries.append(
            SkillSummary(
                skill_id=str(skill.id),
                id=skill_ref(skill, owner_row.handle if owner_row else None),
                tier=skill.tier,
                slug=skill.slug,
                name=skill.name,
                description=skill.description,
                author=author,
                owner_handle=owner_row.handle if owner_row else None,
                requires=requires,
                version=current_version_number,
                available=summary_available,
                unavailable_reason=summary_reason,
            )
        )
    return summaries


async def resolve_skill(
    session: AsyncSession, org_id: uuid.UUID, user: User, raw_ref: str
) -> ResolvedSkill:
    owner_part, slug = parse_skill_ref(raw_ref)
    skill, owner_handle = await _lookup_skill(session, org_id, owner_part, slug)
    state = await skill_access_state(session, org_id, user, skill)
    if state == "none":
        raise SkillNotFound(raw_ref)
    available, reason, requires = await analyze_requirements(session, org_id, user, skill)
    if state == "owner_deactivated":
        available = False
        reason = OWNER_DEACTIVATED_REASON
    return ResolvedSkill(
        skill=skill,
        ref=skill_ref(skill, owner_handle),
        owner_handle=owner_handle,
        required_refs=requires,
        available=available,
        unavailable_reason=reason,
    )


async def set_skill_visibility_level(session: AsyncSession, skill: Skill, visibility: str) -> Skill:
    if visibility not in ("all", "restricted"):
        raise SkillShareError("visibility must be all or restricted")
    skill.visibility = visibility
    await session.flush()
    return skill


async def grant_skill_visibility(session: AsyncSession, skill: Skill, user: User) -> Skill:
    if skill.tier != "global":
        raise SkillShareError("visibility grants apply to global skills")
    if user.org_id != skill.org_id:
        raise SkillShareError("user belongs to a different org than the skill")
    existing = await session.execute(
        select(SkillVisibility.id).where(
            SkillVisibility.skill_id == skill.id,
            SkillVisibility.user_id == user.id,
        )
    )
    if existing.first() is not None:
        raise SkillShareError(f"the skill is already visible to {user.email}")
    session.add(SkillVisibility(org_id=skill.org_id, skill_id=skill.id, user_id=user.id))
    await session.flush()
    return skill


async def revoke_skill_visibility(session: AsyncSession, skill: Skill, user: User) -> Skill:
    await session.execute(
        delete(SkillVisibility).where(
            SkillVisibility.skill_id == skill.id, SkillVisibility.user_id == user.id
        )
    )
    await session.flush()
    return skill


async def list_skill_shares(session: AsyncSession, skill: Skill) -> list[SkillShare]:
    result = await session.execute(
        select(SkillShare)
        .where(SkillShare.skill_id == skill.id)
        .order_by(SkillShare.created_at, SkillShare.id)
    )
    return list(result.scalars())


async def add_skill_share(
    session: AsyncSession, skill: Skill, *, grantee: User, actor: User
) -> SkillShare:
    if skill.tier != "personal":
        raise SkillShareError("global skills are shared through visibility, not shares")
    if skill.owner_user_id != actor.id:
        raise SkillShareError("only the owner can share a personal skill")
    if grantee.org_id != skill.org_id:
        raise SkillShareError("grantee user not found in this org")
    if grantee.id == actor.id:
        raise SkillShareError("cannot share with yourself")
    if grantee.status != "active" or grantee.deactivated_at is not None:
        raise SkillShareError(f"user {grantee.email} is not active")
    existing = await session.execute(
        select(SkillShare.id).where(
            SkillShare.skill_id == skill.id,
            SkillShare.grantee_user_id == grantee.id,
            SkillShare.revoked_at.is_(None),
        )
    )
    if existing.first() is not None:
        raise SkillShareError(f"the skill is already shared with {grantee.email}")
    share = SkillShare(
        org_id=skill.org_id,
        skill_id=skill.id,
        grantee_user_id=grantee.id,
        created_by=actor.id,
    )
    session.add(share)
    await session.flush()
    return share


async def revoke_skill_share_by_id(
    session: AsyncSession, skill: Skill, share_id: uuid.UUID, actor: User
) -> SkillShare:
    share = await session.get(SkillShare, share_id)
    if share is None or share.skill_id != skill.id:
        raise SkillShareError("share not found")
    if skill.owner_user_id != actor.id:
        raise SkillShareError("only the owner can revoke a skill share")
    if share.revoked_at is not None:
        raise SkillShareError("share is already revoked")
    share.revoked_at = datetime.now(UTC)
    await session.flush()
    return share
