"""Dashboard skill routes: browse, detail, editor saves, versions, rollback, sharing.

Business logic lives in `cam.skills.store` and `cam.skills.validation`; access
decisions come from `cam.access` (tier visibility/share rules plus
required-service access through `has_access`). Skill payloads arrive as JSON
with either editor content (`name`, `description`, `body`, `requires`), a full
file list (`files: [{path, content}]` — a folder upload from the browser), or
a zip bundle (`zip_base64`).
"""

from __future__ import annotations

import base64
import difflib
import uuid
from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.access.skills import (
    ResolvedSkill,
    SkillNotFound,
    SkillShareError,
    add_skill_share,
    list_skill_shares,
    resolve_skill,
    revoke_skill_share_by_id,
    set_skill_visibility_level,
)
from cam.access.skills import (
    list_skills as access_list_skills,
)
from cam.accounts.users import get_user_by_email
from cam.api.deps import SessionIdentity, require_user
from cam.api.errors import ApiProblem
from cam.api.pagination import decode_cursor, encode_cursor, parse_limit
from cam.db.models import Skill, SkillShare, SkillVersion, User
from cam.skills import store
from cam.skills.validation import (
    SkillValidationError,
    derive_slug,
    from_editor,
    from_zip_async,
    parse_bundle_async,
    valid_slug,
)

router = APIRouter(prefix="/api", tags=["skills"])

IdentityDep = Annotated[SessionIdentity, Depends(require_user)]

DIFF_MAX_LINES = 400


class SkillFileIn(BaseModel):
    path: str = Field(min_length=1, max_length=400)
    content: str = Field(max_length=200_000)


class SkillCreateBody(BaseModel):
    tier: str = "personal"
    slug: str | None = Field(default=None, min_length=1, max_length=100)
    visibility: str = "all"
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=4000)
    requires: list[str] = Field(default_factory=list)
    body: str = ""
    notes: str | None = Field(default=None, max_length=2000)
    files: list[SkillFileIn] = Field(default_factory=list[SkillFileIn])
    zip_base64: str | None = Field(default=None, max_length=8_000_000)


class SkillVersionBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=4000)
    requires: list[str] = Field(default_factory=list)
    body: str = ""
    files: list[SkillFileIn] = Field(default_factory=list[SkillFileIn])
    zip_base64: str | None = Field(default=None, max_length=8_000_000)
    notes: str | None = Field(default=None, max_length=2000)


class SkillPatchBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=4000)
    visibility: str | None = None


class RollbackBody(BaseModel):
    version: int = Field(ge=1)


class ShareBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)


@dataclass(frozen=True, slots=True)
class SkillContext:
    owner_handle: str | None
    is_owner: bool
    is_admin: bool
    can_manage: bool
    author: str | None
    group: str


def _validation_problem(exc: SkillValidationError) -> ApiProblem:
    return ApiProblem("VALIDATION_ERROR", str(exc), 422)


def _share_problem(exc: SkillShareError) -> ApiProblem:
    message = str(exc)
    if message == "share not found":
        return ApiProblem("NOT_FOUND", "Not found.", 404)
    return ApiProblem("BAD_REQUEST", message, 400)


def _skill_entry(
    resolved: ResolvedSkill,
    *,
    context: SkillContext,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    skill = resolved.skill
    entry: dict[str, Any] = {
        "skill_id": str(skill.id),
        "id": resolved.ref,
        "tier": skill.tier,
        "slug": skill.slug,
        "name": skill.name,
        "description": skill.description,
        "group": context.group,
        "requires": list(resolved.required_refs),
        "available": resolved.available,
        "unavailable_reason": resolved.unavailable_reason,
        "author": context.author,
        "owner": resolved.owner_handle,
        "visibility": skill.visibility,
    }
    if extra:
        entry.update(extra)
    return entry


async def _context(resolved: ResolvedSkill, identity: SessionIdentity) -> SkillContext:
    skill = resolved.skill
    is_owner = skill.owner_user_id == identity.user.id
    is_admin = identity.user.role == "admin"
    can_manage = is_owner if skill.tier == "personal" else is_admin
    if skill.tier == "global":
        group = "global"
        author: str | None = (
            resolved.owner_user.display_name
            if resolved.owner_user is not None
            else identity.user.display_name
            if is_admin
            else None
        )
    elif is_owner:
        group = "mine"
        author = identity.user.display_name
    else:
        group = "shared"
        author = resolved.owner_user.display_name if resolved.owner_user is not None else None
    return SkillContext(
        owner_handle=resolved.owner_handle,
        is_owner=is_owner,
        is_admin=is_admin,
        can_manage=can_manage,
        author=author,
        group=group,
    )


async def _resolve_by_parts(
    db: AsyncSession, identity: SessionIdentity, owner: str, slug: str
) -> ResolvedSkill:
    try:
        return await resolve_skill(db, identity.org_id, identity.user, f"{owner}/{slug}")
    except SkillNotFound:
        raise ApiProblem("NOT_FOUND", "Skill not found.", 404) from None


async def _version_actor_map(
    db: AsyncSession, versions: list[SkillVersion]
) -> dict[uuid.UUID, User | None]:
    actor_ids = {version.created_by for version in versions if version.created_by}
    actors: dict[uuid.UUID, User] = {}
    if actor_ids:
        rows = await db.execute(select(User).where(User.id.in_(actor_ids)))
        actors = {user.id: user for user in rows.scalars()}
    return {
        version.id: actors.get(version.created_by) if version.created_by else None
        for version in versions
    }


def _version_rows(
    versions: list[SkillVersion], actors: dict[uuid.UUID, User | None]
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for version in versions:
        actor = actors.get(version.id)
        entries.append(
            {
                "id": str(version.id),
                "version": version.version,
                "notes": version.notes,
                "created_at": version.created_at.isoformat() if version.created_at else None,
                "created_by_name": actor.display_name if actor else None,
                "created_by_email": actor.email if actor else None,
            }
        )
    return entries


async def _share_entries(db: AsyncSession, shares: list[SkillShare]) -> list[dict[str, Any]]:
    grantee_ids = {share.grantee_user_id for share in shares if share.grantee_user_id}
    grantees: dict[uuid.UUID, User] = {}
    if grantee_ids:
        rows = await db.execute(select(User).where(User.id.in_(grantee_ids)))
        grantees = {user.id: user for user in rows.scalars()}
    entries: list[dict[str, Any]] = []
    for share in shares:
        grantee = grantees.get(share.grantee_user_id)
        entries.append(
            {
                "id": str(share.id),
                "created_at": share.created_at.isoformat() if share.created_at else None,
                "revoked_at": share.revoked_at.isoformat() if share.revoked_at else None,
                "email": grantee.email if grantee else None,
                "display_name": grantee.display_name if grantee else None,
            }
        )
    return entries


async def _files_public(db: AsyncSession, version: SkillVersion | None) -> list[dict[str, Any]]:
    if version is None:
        return []
    files = await store.version_files(db, version.id)
    return [
        {"path": path, "content": content, "bytes": len(content.encode("utf-8"))}
        for path, content in sorted(files.items())
    ]


async def _source_from_body(body: SkillCreateBody | SkillVersionBody) -> Any:
    if body.zip_base64 is not None:
        try:
            data = base64.b64decode(body.zip_base64, validate=True)
        except ValueError:
            raise ApiProblem("VALIDATION_ERROR", "invalid zip_base64", 422) from None
        return await from_zip_async(data)
    if body.files:
        return await parse_bundle_async(
            {item.path: item.content.encode("utf-8") for item in body.files}
        )
    if body.name is None or body.description is None:
        raise ApiProblem(
            "VALIDATION_ERROR", "name and description are required (or upload files)", 422
        )
    return from_editor(
        name=body.name, description=body.description, requires=body.requires, body=body.body
    )


@router.get("/skills")
async def get_skills(
    request: Request,
    identity: IdentityDep,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    resolved_limit = parse_limit(limit)
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        summaries = await access_list_skills(db, identity.org_id, identity.user)
    summaries.sort(key=lambda summary: summary.id)
    page = summaries
    next_cursor = None
    if cursor is not None:
        parts = decode_cursor(cursor, count=1)
        if parts is None or not parts:
            raise ApiProblem("VALIDATION_ERROR", "invalid cursor", 422)
        page = [summary for summary in summaries if summary.id > parts[0]]
    if len(page) > resolved_limit:
        page = page[:resolved_limit]
        next_cursor = encode_cursor(page[-1].id)
    entries = [
        {
            "skill_id": summary.skill_id,
            "id": summary.id,
            "tier": summary.tier,
            "slug": summary.slug,
            "name": summary.name,
            "description": summary.description,
            "group": "global"
            if summary.tier == "global"
            else "mine"
            if summary.owner_handle == identity.user.handle
            else "shared",
            "requires": list(summary.requires),
            "available": summary.available,
            "unavailable_reason": summary.unavailable_reason,
            "author": summary.author,
            "owner": summary.owner_handle,
            "version": summary.version,
        }
        for summary in page
    ]
    return {"skills": entries, "next_cursor": next_cursor}


@router.post("/skills")
async def create_skill(
    request: Request, identity: IdentityDep, body: SkillCreateBody
) -> dict[str, Any]:
    if body.tier not in ("personal", "global"):
        raise ApiProblem("VALIDATION_ERROR", "tier must be personal or global", 422)
    if body.tier == "global" and identity.user.role != "admin":
        raise ApiProblem("FORBIDDEN", "Only admins can add global skills.", 403)
    if body.zip_base64 is not None and body.files:
        raise ApiProblem("VALIDATION_ERROR", "send either zip_base64 or files", 422)
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            source = await _source_from_body(body)
            slug = valid_slug(body.slug) if body.slug else derive_slug(source.name)
            skill, version = await store.create_skill(
                db,
                org_id=identity.org_id,
                tier=body.tier,
                owner_user_id=identity.user.id if body.tier == "personal" else None,
                actor_user_id=identity.user.id,
                slug=slug,
                source=source,
                visibility=body.visibility,
                notes=body.notes,
            )
            await db.commit()
        except SkillValidationError as exc:
            await db.rollback()
            raise _validation_problem(exc) from None
        ref = (
            f"global/{skill.slug}"
            if skill.tier == "global"
            else f"{identity.user.handle}/{skill.slug}"
        )
    return {"skill": {"skill_id": str(skill.id), "id": ref, "version": version.version}}


async def _skill_detail(
    db: AsyncSession, identity: SessionIdentity, resolved: ResolvedSkill
) -> dict[str, Any]:
    context = await _context(resolved, identity)
    skill = resolved.skill
    version = await store.current_version(db, skill)
    if version is not None:
        versions = await store.list_versions(db, skill)
    else:
        versions = []
    actors = await _version_actor_map(db, versions)
    files = await _files_public(db, version)
    extra = {
        "version": version.version if version is not None else None,
        "created_at": skill.created_at.isoformat() if skill.created_at else None,
        "files": files,
        "viewer": {"is_owner": context.is_owner, "can_manage": context.can_manage},
    }
    return {
        "skill": _skill_entry(resolved, context=context, extra=extra),
        "versions": _version_rows(versions, actors),
    }


@router.get("/skills/{owner}/{slug}/versions/{version_number}")
async def get_skill_version_detail(
    request: Request, identity: IdentityDep, owner: str, slug: str, version_number: int
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        resolved = await _resolve_by_parts(db, identity, owner, slug)
        version = await store.version_by_number(db, resolved.skill, version_number)
        if version is None:
            raise ApiProblem("NOT_FOUND", "Version not found.", 404)
        files = await _files_public(db, version)
    return {
        "version": {
            "id": str(version.id),
            "version": version.version,
            "notes": version.notes,
            "files": files,
        }
    }


@router.get("/skills/{owner}/{slug}")
async def get_skill_detail(
    request: Request, identity: IdentityDep, owner: str, slug: str
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        resolved = await _resolve_by_parts(db, identity, owner, slug)
        return await _skill_detail(db, identity, resolved)


@router.patch("/skills/{owner}/{slug}")
async def patch_skill(
    request: Request, identity: IdentityDep, owner: str, slug: str, body: SkillPatchBody
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        resolved = await _resolve_by_parts(db, identity, owner, slug)
        context = await _context(resolved, identity)
        if not context.can_manage:
            raise ApiProblem("FORBIDDEN", "You cannot change this skill.", 403)
        skill = resolved.skill
        try:
            if body.name is not None and body.name.strip():
                skill.name = body.name.strip()
            if body.description is not None and body.description.strip():
                skill.description = body.description
            if body.visibility is not None:
                if skill.tier != "global":
                    raise SkillShareError("visibility applies to global skills only")
                await set_skill_visibility_level(db, skill, body.visibility)
            await db.commit()
        except SkillShareError as exc:
            await db.rollback()
            raise _share_problem(exc) from None
        detail = await _skill_detail(db, identity, resolved)
    return {"skill": detail["skill"]}


@router.delete("/skills/{owner}/{slug}")
async def delete_skill(
    request: Request, identity: IdentityDep, owner: str, slug: str
) -> dict[str, bool]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        resolved = await _resolve_by_parts(db, identity, owner, slug)
        context = await _context(resolved, identity)
        if not context.can_manage:
            raise ApiProblem("FORBIDDEN", "You cannot delete this skill.", 403)
        await store.delete_skill(db, resolved.skill)
        await db.commit()
    return {"ok": True}


@router.get("/skills/{owner}/{slug}/versions")
async def get_skill_versions(
    request: Request, identity: IdentityDep, owner: str, slug: str
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        resolved = await _resolve_by_parts(db, identity, owner, slug)
        versions = await store.list_versions(db, resolved.skill)
        actors = await _version_actor_map(db, versions)
    return {"versions": _version_rows(versions, actors)}


@router.post("/skills/{owner}/{slug}/versions")
async def create_skill_version(
    request: Request, identity: IdentityDep, owner: str, slug: str, body: SkillVersionBody
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        resolved = await _resolve_by_parts(db, identity, owner, slug)
        context = await _context(resolved, identity)
        if not context.can_manage:
            raise ApiProblem("FORBIDDEN", "You cannot change this skill.", 403)
        try:
            source = await _source_from_body(body)
            if body.zip_base64 is None and not body.files:
                merged = dict(await store.current_files(db, resolved.skill))
                merged.update(source.files)
                source = await parse_bundle_async(
                    {path: content.encode("utf-8") for path, content in merged.items()}
                )
            version = await store.add_version(
                db,
                resolved.skill,
                source=source,
                actor_user_id=identity.user.id,
                notes=body.notes,
            )
            await db.commit()
        except SkillValidationError as exc:
            await db.rollback()
            raise _validation_problem(exc) from None
    return {"version": {"id": str(version.id), "version": version.version}}


@router.post("/skills/{owner}/{slug}/rollback")
async def rollback_skill(
    request: Request, identity: IdentityDep, owner: str, slug: str, body: RollbackBody
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        resolved = await _resolve_by_parts(db, identity, owner, slug)
        context = await _context(resolved, identity)
        if not context.can_manage:
            raise ApiProblem("FORBIDDEN", "You cannot change this skill.", 403)
        target = await store.version_by_number(db, resolved.skill, body.version)
        if target is None:
            raise ApiProblem("NOT_FOUND", "Version not found.", 404)
        version = await store.rollback_to_version(
            db,
            resolved.skill,
            target=target,
            actor_user_id=identity.user.id,
            notes=f"rollback to version {body.version}",
        )
        await db.commit()
    return {"version": {"id": str(version.id), "version": version.version}}


@router.get("/skills/{owner}/{slug}/diff")
async def get_version_diff(
    request: Request,
    identity: IdentityDep,
    owner: str,
    slug: str,
    from_version: int,
    to: int | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        resolved = await _resolve_by_parts(db, identity, owner, slug)
        base = await store.version_by_number(db, resolved.skill, from_version)
        if base is None:
            raise ApiProblem("NOT_FOUND", "Version not found.", 404)
        target = (
            await store.version_by_number(db, resolved.skill, to)
            if to is not None
            else await store.current_version(db, resolved.skill)
        )
        if target is None:
            raise ApiProblem("NOT_FOUND", "Version not found.", 404)
        base_files = await store.version_files(db, base.id)
        target_files = await store.version_files(db, target.id)
        target_path = path if path is not None else "SKILL.md"
        if target_path not in base_files and target_path not in target_files:
            raise ApiProblem("NOT_FOUND", "File not found in these versions.", 404)
        diff = difflib.unified_diff(
            base_files.get(target_path, "").splitlines(),
            target_files.get(target_path, "").splitlines(),
            fromfile=f"v{base.version}/{target_path}",
            tofile=f"v{target.version}/{target_path}",
            lineterm="",
        )
        payload_diff: list[str] = []
        for index, line in enumerate(diff):
            if index >= DIFF_MAX_LINES:
                break
            payload_diff.append(line)
    return {
        "path": target_path,
        "from_version": base.version,
        "to_version": target.version,
        "diff": cast("list[Any]", payload_diff),
        "truncated": len(payload_diff) > DIFF_MAX_LINES,
    }


async def _shares_payload(db: AsyncSession, skill: Skill) -> list[dict[str, Any]]:
    shares = await list_skill_shares(db, skill)
    return await _share_entries(db, shares)


@router.get("/skills/{owner}/{slug}/shares")
async def get_skill_shares(
    request: Request, identity: IdentityDep, owner: str, slug: str
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        resolved = await _resolve_by_parts(db, identity, owner, slug)
        context = await _context(resolved, identity)
        if not context.can_manage:
            raise ApiProblem("FORBIDDEN", "Only the skill owner can manage shares.", 403)
        return {"shares": await _shares_payload(db, resolved.skill)}


@router.post("/skills/{owner}/{slug}/shares")
async def create_skill_share(
    request: Request, identity: IdentityDep, owner: str, slug: str, body: ShareBody
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        resolved = await _resolve_by_parts(db, identity, owner, slug)
        skill = resolved.skill
        if skill.owner_user_id != identity.user.id:
            raise ApiProblem("NOT_FOUND", "Skill not found.", 404)
        grantee = await get_user_by_email(db, body.email.strip().lower())
        if grantee is None:
            raise ApiProblem("BAD_REQUEST", f"no user with email {body.email}", 400)
        try:
            share = await add_skill_share(db, skill, grantee=grantee, actor=identity.user)
            await db.commit()
        except SkillShareError as exc:
            await db.rollback()
            raise _share_problem(exc) from None
    return {
        "share": {
            "id": str(share.id),
            "created_at": share.created_at.isoformat() if share.created_at else None,
            "revoked_at": None,
            "email": grantee.email,
            "display_name": grantee.display_name,
        }
    }


@router.delete("/skill-shares/{share_id}")
async def delete_skill_share(
    request: Request, identity: IdentityDep, share_id: uuid.UUID
) -> dict[str, bool]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        share = await db.get(SkillShare, share_id)
        if share is None or share.org_id != identity.org_id:
            raise ApiProblem("NOT_FOUND", "Not found.", 404)
        skill = await db.get(Skill, share.skill_id)
        if skill is None:
            raise ApiProblem("NOT_FOUND", "Not found.", 404)
        try:
            await revoke_skill_share_by_id(db, skill, share_id, identity.user)
            await db.commit()
        except SkillShareError as exc:
            await db.rollback()
            raise _share_problem(exc) from None
    return {"ok": True}
