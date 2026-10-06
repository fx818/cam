"""Admin "needs attention" digest: everything that needs a human today.

Pure read-side aggregation over existing tables; thresholds come from
`orgs.settings` (`credential_stale_days`, default 90) so nothing org-specific
lands in code. It reports; it never acts.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.api.deps import SessionIdentity
from cam.db.models import CamToken, Connection, Org, Service, Tool, User


def _int_setting(settings: Mapping[str, Any], key: str, default: int) -> int:
    value = settings.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default


def _days_since(ts: datetime | None, now: datetime) -> int | None:
    if ts is None:
        return None
    return (now - ts).days


async def admin_attention_items(
    session: AsyncSession, identity: SessionIdentity
) -> list[dict[str, Any]]:
    org = await session.get(Org, identity.org_id)
    settings = (org.settings or {}) if org is not None else {}
    stale_days = _int_setting(settings, "credential_stale_days", 90)
    now = datetime.now(tz=UTC)
    items: list[dict[str, Any]] = []

    review_rows = (
        await session.execute(
            select(Tool.name, Tool.created_at, Service.name)
            .join(Service, Service.id == Tool.service_id)
            .where(
                Tool.org_id == identity.org_id,
                Tool.status == "pending_review",
                Tool.enabled.is_(False),
            )
            .order_by(Tool.created_at)
        )
    ).all()
    for tool_name, created_at, service_name in review_rows:
        age = _days_since(created_at, now)
        age_text = f", oldest waiting {age} day(s)" if age is not None else ""
        items.append(
            {
                "kind": "review_queue",
                "label": f'{service_name}: tool "{tool_name}" awaits approval',
                "detail": f"{len(review_rows)} tool(s) in the review queue{age_text}.",
                "href": "/admin/review-queue",
            }
        )

    reconnect_rows = (
        await session.execute(
            select(Service.name, Connection.user_id, Connection.expires_at, User.email)
            .outerjoin(User, User.id == Connection.user_id)
            .where(
                Connection.org_id == identity.org_id,
                Connection.status == "needs_reconnect",
            )
            .order_by(Service.name)
        )
    ).all()
    for service_name, user_id, expires_at, owner_email in reconnect_rows:
        whose = owner_email or ("the org credential" if user_id is None else "an unknown user")
        expires_text = (
            f" token expired {expires_at.date().isoformat()}" if expires_at is not None else ""
        )
        items.append(
            {
                "kind": "reconnect",
                "label": f"{service_name}: needs reconnect ({whose})",
                "detail": f"Connection status needs_reconnect{expires_text}.",
                "href": "/admin/services",
            }
        )

    failing_rows = (
        await session.execute(
            select(Service.name).where(
                Service.org_id == identity.org_id,
                Service.status == "active",
                Service.health == "failing",
            )
        )
    ).all()
    for (service_name,) in failing_rows:
        items.append(
            {
                "kind": "failing_service",
                "label": f"{service_name}: upstream failing",
                "detail": "Health shows failing; check the credential or URL.",
                "href": "/admin/services",
            }
        )

    cutoff = now - timedelta(days=stale_days)
    stale_rows = (
        await session.execute(
            select(Connection.last_rotated_at, Service.name)
            .join(Service, Service.id == Connection.service_id)
            .where(
                Connection.org_id == identity.org_id,
                Connection.status == "connected",
                Connection.user_id.is_(None),
                Service.auth_type != "none",
                Connection.secret_enc.isnot(None),
                Connection.last_rotated_at.is_(None),
            )
            .order_by(Service.name)
        )
    ).all()
    never_rotated_rows = [
        (row[0], row[1])
        for row in stale_rows  # last_rotated_at NULL → never
    ]
    rotated_rows = (
        await session.execute(
            select(Connection.last_rotated_at, Service.name)
            .join(Service, Service.id == Connection.service_id)
            .where(
                Connection.org_id == identity.org_id,
                Connection.status == "connected",
                Connection.user_id.is_(None),
                Service.auth_type != "none",
                Connection.secret_enc.isnot(None),
                Connection.last_rotated_at.isnot(None),
                Connection.last_rotated_at < cutoff,
            )
            .order_by(Service.name)
        )
    ).all()
    for _last, service_name in never_rotated_rows:
        items.append(
            {
                "kind": "stale_credential",
                "label": f"{service_name}: org credential never rotated",
                "detail": f"Threshold is {stale_days} days.",
                "href": "/admin/services",
            }
        )
    for last, service_name in rotated_rows:
        age = _days_since(last, now)
        items.append(
            {
                "kind": "stale_credential",
                "label": f"{service_name}: org credential stale ({age} d)",
                "detail": f"Threshold is {stale_days} days.",
                "href": "/admin/services",
            }
        )

    active_users = (
        await session.execute(
            select(User.id, User.email, User.display_name).where(
                User.org_id == identity.org_id,
                User.status == "active",
                User.role == "user",
            )
        )
    ).all()
    token_map: dict[uuid.UUID, datetime | None] = dict()
    if active_users:
        user_ids = [row[0] for row in active_users]
        token_rows = (
            await session.execute(
                select(CamToken.user_id, CamToken.last_used_at).where(
                    CamToken.org_id == identity.org_id,
                    CamToken.revoked_at.is_(None),
                    CamToken.user_id.in_(user_ids),
                )
            )
        ).all()
        for user_id, last_used_at in token_rows:
            if user_id in token_map and token_map[user_id] is not None:
                continue
            token_map[user_id] = last_used_at
    for user_id, email, display_name in active_users:
        if token_map.get(user_id) is None:
            items.append(
                {
                    "kind": "activation",
                    "label": f"{display_name or email}: no token or never invoked",
                    "detail": "Enrollment gap: the account has not used CAM yet.",
                    "href": "/admin/users",
                }
            )
    return items
