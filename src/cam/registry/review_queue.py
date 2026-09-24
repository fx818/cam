"""Admin review queue: tools found on global and template re-syncs wait here
as `pending_review`. Listing shows every pending tool of the org with its
service; bulk approval flips tools to `active`+enabled, which makes them
listable, describable and invokable at once.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.db.models import Service, Tool

ReviewTier = Literal["global", "template"]


@dataclass(frozen=True, slots=True)
class ReviewQueueEntry:
    tool_id: uuid.UUID
    name: str
    description: str | None
    service_id: uuid.UUID
    service_ref: str
    service_name: str
    tier: ReviewTier
    status: str
    enabled: bool

    def payload(self) -> dict[str, object]:
        return {
            "id": str(self.tool_id),
            "name": self.name,
            "description": self.description,
            "service_id": str(self.service_id),
            "service_ref": self.service_ref,
            "service_name": self.service_name,
            "tier": self.tier,
            "status": self.status,
            "enabled": self.enabled,
        }


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    approved: tuple[uuid.UUID, ...]
    unknown: tuple[uuid.UUID, ...]


def _display_description(tool: Tool) -> str | None:
    if tool.description_override:
        return tool.description_override
    return tool.description


async def list_pending_tools(
    session: AsyncSession, *, org_id: uuid.UUID, limit: int = 200
) -> list[ReviewQueueEntry]:
    rows = (
        await session.execute(
            select(Tool, Service)
            .join(Service, Service.id == Tool.service_id)
            .where(
                Tool.org_id == org_id,
                Tool.status == "pending_review",
                Service.tier.in_(("global", "template")),
                Service.status != "deleted",
            )
            .order_by(Tool.created_at, Tool.id)
            .limit(limit)
        )
    ).all()
    entries: list[ReviewQueueEntry] = []
    for tool, service in rows:
        tier: ReviewTier = "template" if service.tier == "template" else "global"
        entries.append(
            ReviewQueueEntry(
                tool_id=tool.id,
                name=tool.name,
                description=_display_description(tool),
                service_id=service.id,
                service_ref=f"global/{service.slug}",
                service_name=service.name,
                tier=tier,
                status=tool.status,
                enabled=tool.enabled,
            )
        )
    return entries


async def approve_tools(
    session: AsyncSession, *, org_id: uuid.UUID, tool_ids: list[uuid.UUID]
) -> ApprovalResult:
    unique: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for tool_id in tool_ids:
        if tool_id not in seen:
            seen.add(tool_id)
            unique.append(tool_id)
    approved: list[uuid.UUID] = []
    for tool_id in unique:
        tool = await session.get(Tool, tool_id)
        if tool is None or tool.org_id != org_id or tool.status != "pending_review":
            continue
        tool.status = "active"
        tool.enabled = True
        approved.append(tool.id)
    await session.flush()
    approved_set = set(approved)
    return ApprovalResult(
        approved=tuple(approved),
        unknown=tuple(tool_id for tool_id in unique if tool_id not in approved_set),
    )
