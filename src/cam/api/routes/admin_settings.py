from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from cam.access.resolution import ServiceNotFound
from cam.api.deps import SessionIdentity, require_admin
from cam.api.errors import ApiProblem
from cam.api.queries import (
    add_allowlist_entry,
    delete_allowlist_entry,
    list_allowlist_entries,
    org_settings_public,
    update_org_settings,
)
from cam.api.settings_resolver import effective_settings
from cam.connections.url_policy import AllowlistError
from cam.db.models import Org

router = APIRouter(prefix="/api/admin", tags=["admin", "admin-settings"])

AdminDep = Annotated[SessionIdentity, Depends(require_admin)]


class SettingsPatchBody(BaseModel):
    default_timeout_ms: int | None = Field(default=None, ge=1000, le=600_000)
    response_cap_tokens: int | None = Field(default=None, ge=100, le=1_000_000)
    log_retention_days: int | None = Field(default=None, ge=1, le=3650)
    allowed_email_domains: list[str] | None = None
    # Canonical name (what the gateway/resolver read); `instructions` kept as a
    # deprecated alias for the current dashboard form.
    server_instructions: str | None = Field(default=None, max_length=20_000)
    instructions: str | None = Field(default=None, max_length=20_000)
    brand: dict[str, Any] | None = None


class AllowlistCreateBody(BaseModel):
    pattern: str = Field(min_length=1, max_length=255)
    note: str | None = Field(default=None, max_length=1000)


@router.get("/settings")
async def get_settings(request: Request, admin: AdminDep) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        org = await db.get(Org, admin.org_id)
        assert org is not None
        effective = await effective_settings(db, admin.org_id)
        return {"settings": org_settings_public(org), "effective": asdict(effective)}


@router.patch("/settings")
async def patch_settings(
    request: Request, admin: AdminDep, body: SettingsPatchBody
) -> dict[str, object]:
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise ApiProblem("VALIDATION_ERROR", "No fields to update.", 422)
    if updates.get("allowed_email_domains") is not None:
        domains = [d.strip().lower() for d in updates["allowed_email_domains"] if d.strip()]
        updates["allowed_email_domains"] = domains
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        settings = await update_org_settings(db, admin, updates)
        await db.commit()
        effective = await effective_settings(db, admin.org_id)
    return {"settings": settings, "effective": asdict(effective)}


@router.get("/url-allowlist")
async def get_allowlist(request: Request, admin: AdminDep) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        entries = await list_allowlist_entries(db, admin)
    return {"entries": entries}


@router.post("/url-allowlist", status_code=201)
async def post_allowlist(
    request: Request, admin: AdminDep, body: AllowlistCreateBody
) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            entry = await add_allowlist_entry(db, admin, pattern=body.pattern, note=body.note)
        except AllowlistError as exc:
            raise ApiProblem("VALIDATION_ERROR", str(exc), 422) from None
        await db.commit()
    return {"entry": entry}


@router.delete("/url-allowlist/{entry_id}")
async def delete_allowlist(
    request: Request, admin: AdminDep, entry_id: uuid.UUID
) -> dict[str, bool]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            await delete_allowlist_entry(db, admin, entry_id)
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "Allowlist entry not found.", 404) from None
        await db.commit()
    return {"ok": True}
