"""The cam tool skill actions: list_skills, load_skill, read_skill_file.

All access decisions come from `cam.access.skills`; this module only shapes the
agent-facing payloads and maps access failures to CAM error codes. Skill loads
and file reads are recorded through the audit writer with the version.
"""

from __future__ import annotations

from typing import Any, NoReturn

from sqlalchemy.ext.asyncio import AsyncSession

from cam.access.skills import SkillNotFound, list_skills, resolve_skill
from cam.audit.writer import AuditRef
from cam.db.models import User
from cam.invoke.errors import CamError, CamErrorCode, invalid_args, not_found
from cam.skills import store
from cam.skills.validation import SkillValidationError as InvalidPath
from cam.skills.validation import normalize_path


def require_skill_id(raw: Any, field: str = "id") -> str:
    if raw is None:
        raise invalid_args(field, "is required")
    if not isinstance(raw, str):
        raise invalid_args(field, "must be a string")
    return raw


def _not_found() -> NoReturn:
    raise not_found()


def _skill_unavailable(reason: str, skill_version_id: Any) -> NoReturn:
    raise CamError(
        CamErrorCode.SKILL_UNAVAILABLE,
        f"This skill is unavailable: {reason}",
        audit_ref=AuditRef(
            service_id=None,
            skill_version_id=skill_version_id if skill_version_id is not None else None,
        ),
    )


async def _resolve_or_raise(session: AsyncSession, org_id: Any, user: User, raw_ref: str) -> Any:
    try:
        resolved = await resolve_skill(session, org_id, user, raw_ref)
    except SkillNotFound:
        _not_found()
    return resolved


async def list_skills_payload(session_factory: Any, identity: Any) -> dict[str, Any]:
    async with session_factory() as session:
        summaries = await list_skills(session, identity.org_id, identity.user)
    return {
        "skills": [
            {
                "id": summary.id,
                "name": summary.name,
                "description": summary.description,
                "author": summary.author,
                "available": summary.available,
                "unavailable_reason": summary.unavailable_reason,
            }
            for summary in summaries
        ]
    }


async def load_skill_payload(
    session: AsyncSession, org_id: Any, user: User, raw_id: Any
) -> tuple[dict[str, Any], Any]:
    skill_id = require_skill_id(raw_id)
    resolved = await _resolve_or_raise(session, org_id, user, skill_id)
    if not resolved.available:
        _skill_unavailable(
            resolved.unavailable_reason or "no access", resolved.skill.current_version_id
        )
    version = await store.current_version(session, resolved.skill)
    if version is None:
        _not_found()
    files = await store.version_files(session, version.id)
    payload = {
        "id": resolved.ref,
        "version": version.version,
        "content": files.get("SKILL.md", ""),
        "files": sorted(files.keys()),
    }
    return payload, AuditRef(service_id=None, skill_version_id=version.id)


async def read_skill_file_payload(
    session: AsyncSession, org_id: Any, user: User, raw_id: Any, raw_path: Any
) -> tuple[dict[str, Any], Any]:
    skill_id = require_skill_id(raw_id)
    if raw_path is None:
        raise invalid_args("path", "is required")
    if not isinstance(raw_path, str):
        raise invalid_args("path", "must be a string")
    try:
        path = normalize_path(raw_path)
    except InvalidPath:
        _not_found()
    resolved = await _resolve_or_raise(session, org_id, user, skill_id)
    if not resolved.available:
        _skill_unavailable(
            resolved.unavailable_reason or "no access", resolved.skill.current_version_id
        )
    version = await store.current_version(session, resolved.skill)
    if version is None:
        _not_found()
    files = await store.version_files(session, version.id)
    if path not in files:
        _not_found()
    payload = {"id": resolved.ref, "path": path, "content": files[path]}
    return payload, AuditRef(service_id=None, skill_version_id=version.id)
