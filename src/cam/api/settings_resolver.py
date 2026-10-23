"""Typed effective org settings resolver.

One place turns the free-form ``orgs.settings`` JSON into a typed
:class:`OrgEffective`. A ``None`` field means "unset", so every caller
applies its platform default with one precedence rule (org override wins,
otherwise the platform/env default).

Deliberately import-light: only stdlib, SQLAlchemy and the ``Org`` model, so
the gateway, jobs and API layers can all import it without cycles.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.db.models import Org


@dataclass(frozen=True, slots=True)
class OrgEffective:
    """Resolved ``orgs.settings`` values; ``None`` means the key is unset."""

    server_instructions: str | None = None
    log_retention_days: int | None = None
    default_timeout_s: int | None = None
    default_response_cap_tokens: int | None = None
    credential_stale_days: int | None = None


def _as_int(value: Any) -> int | None:
    # bool is an int subclass; never treat a flag as a count.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.lstrip("-").isdigit():
            try:
                return int(candidate)
            except ValueError:  # pragma: no cover - guarded by isdigit
                return None
    return None


def _as_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def org_effective_from_settings(settings: dict[str, Any] | None) -> OrgEffective:
    """Pure mapping from an ``orgs.settings`` mapping (no DB access)."""
    data = settings or {}
    return OrgEffective(
        server_instructions=_as_str(data.get("server_instructions")),
        log_retention_days=_as_int(data.get("log_retention_days")),
        default_timeout_s=_as_int(data.get("default_timeout_s")),
        default_response_cap_tokens=_as_int(data.get("default_response_cap_tokens")),
        credential_stale_days=_as_int(data.get("credential_stale_days")),
    )


async def effective_settings(session: AsyncSession, org_id: uuid.UUID) -> OrgEffective:
    """Read the caller's org settings and return the typed effective view."""
    settings = (
        await session.execute(select(Org.settings).where(Org.id == org_id))
    ).scalar_one_or_none()
    return org_effective_from_settings(settings)
