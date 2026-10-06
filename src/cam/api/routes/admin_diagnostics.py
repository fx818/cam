"""Admin diagnostics: run a tool under your own access and explain failures.

`POST /api/admin/tools/{tool_id}/invoke` reuses the exact agent pipeline
(resolve → access → schema validation → dispatch) as the admin's own user, so
the exercised path and the audit rows match what an agent would produce.

`POST /api/admin/audit/{entry_id}/explain` replays a stored failure read-only
with a per-stage report, labelled "as of now" (state is re-read, not
time-travelled).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select

from cam.access.resolution import ServiceNotFound
from cam.api.deps import SessionIdentity, require_admin
from cam.api.errors import ApiProblem
from cam.api.queries import dashboard_viewer_identity, get_tool_context
from cam.db.models import AuditLog, Connection, Service, Tool
from cam.invoke.pipeline import AgentInfo, handle
from cam.upstream.types import TextBlock

router = APIRouter(prefix="/api/admin", tags=["admin", "admin-diagnostics"])

AdminDep = Annotated[SessionIdentity, Depends(require_admin)]


class ToolInvokeBody(BaseModel):
    args: dict[str, Any] = {}


def _text_of(outcome: Any) -> str:
    return "\n".join(block.text for block in outcome.content if isinstance(block, TextBlock))


@router.post("/tools/{tool_id}/invoke")
async def admin_invoke_tool(
    request: Request,
    admin: AdminDep,
    tool_id: uuid.UUID,
    body: ToolInvokeBody,
) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        try:
            tool, context = await get_tool_context(db, admin, tool_id)
        except ServiceNotFound:
            raise ApiProblem("NOT_FOUND", "Tool not found.", 404) from None
        if not context.can_manage:
            raise ApiProblem("FORBIDDEN", "You cannot invoke this service's tools.", 403)
        tool_id_ref = f"{context.ref}/{tool.name}"
        stored_example = tool.example_invocation

    args = dict(body.args or {})
    if not args and isinstance(stored_example, dict):
        args = dict(stored_example)

    deps = getattr(request.app.state, "deps", None)
    if deps is None:
        raise ApiProblem("INTERNAL", "Gateway is not initialised.", 500)
    outcome = await handle(
        deps,
        dashboard_viewer_identity(admin),
        AgentInfo(name="cam-dashboard"),
        action="invoke",
        id=tool_id_ref,
        args=args,
    )
    return {
        "invoke": {
            "is_error": outcome.is_error,
            "truncated": outcome.truncated,
            "text": _text_of(outcome),
            "structured": outcome.structured,
        },
        "args_sent": args,
        "tool_ref": tool_id_ref,
    }


@router.post("/audit/{entry_id}/explain")
async def explain_audit_entry(
    request: Request,
    admin: AdminDep,
    entry_id: uuid.UUID,
) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        entry = await db.get(AuditLog, entry_id)
        if entry is None or entry.org_id != admin.org_id:
            raise ApiProblem("NOT_FOUND", "Activity entry not found.", 404)
        stages = await _explain_stages(db, entry)
    return {"entry_id": str(entry_id), "explain": stages}


_ERROR_EXPLANATIONS: dict[str, str] = {
    "UPSTREAM_AUTH": (
        "The upstream rejected the stored credential: rotate the "
        "connection's secret or reconnect the OAuth connection."
    ),
    "CONNECTION_EXPIRED": (
        "The connection token expired and the refresh failed; the owner "
        "must reconnect from the dashboard connection tab."
    ),
    "TIMEOUT": "The upstream did not answer inside the service timeout window.",
    "UPSTREAM_UNAVAILABLE": "The upstream could not be reached or returned a 5xx.",
    "UPSTREAM_RATE_LIMITED": "The upstream throttled this caller; retry later.",
    "UPSTREAM_ERROR": "The upstream answered but reported a tool-level failure.",
    "INVALID_ARGS": "The call arguments failed schema validation.",
    "CAM_RATE_LIMITED": "This token exceeded CAM's per-minute invoke rate limit.",
}


async def _explain_stages(db: Any, entry: AuditLog) -> list[dict[str, str]]:
    stages: list[dict[str, str]] = [
        {
            "stage": "note",
            "result": "ok",
            "detail": "Read-only replay of current state, not the state at log time.",
        }
    ]
    if entry.status == "error" and entry.error_code:
        explanation = _ERROR_EXPLANATIONS.get(entry.error_code)
        stages.append(
            {
                "stage": "logged_error",
                "result": "error",
                "detail": f"{entry.error_code} — {entry.error_message or ''}"
                + (f" Fix: {explanation}" if explanation else ""),
            }
        )
    else:
        stages.append(
            {
                "stage": "logged_error",
                "result": "ok",
                "detail": "The logged call succeeded; nothing failed.",
            }
        )

    service: Service | None = None
    if entry.service_id is None:
        stages.append(
            {
                "stage": "service",
                "result": "n/a",
                "detail": "No service recorded: the call failed before access resolution.",
            }
        )
    else:
        service = await db.get(Service, entry.service_id)
        if service is None or service.status == "deleted":
            stages.append(
                {"stage": "service", "result": "removed", "detail": "The service no longer exists."}
            )
        else:
            stages.append(
                {
                    "stage": "service",
                    "result": "ok",
                    "detail": (
                        f"{service.name} (tier {service.tier}, "
                        f"status {service.status}, health {service.health})."
                    ),
                }
            )

    tool_service_id = entry.service_id
    if entry.tool_name and tool_service_id is not None:
        tool_row = (
            await db.execute(
                select(Tool).where(
                    Tool.service_id == tool_service_id,
                    Tool.name == entry.tool_name,
                )
            )
        ).scalar_one_or_none()
        if tool_row is None:
            stages.append(
                {
                    "stage": "tool",
                    "result": "missing",
                    "detail": "The tool is no longer registered for the service.",
                }
            )
        else:
            detail = f"Name {tool_row.name}, status {tool_row.status}, enabled {tool_row.enabled}."
            if tool_row.status == "removed_upstream":
                detail += " The tool disappeared upstream on a re-sync; re-approve or re-sync."
            elif not tool_row.enabled:
                detail += " An admin excluded this tool."
            elif tool_row.status == "pending_review":
                detail += " Tool still awaits review-queue approval."
            stages.append({"stage": "tool", "result": "ok", "detail": detail})

        if (
            tool_row is not None
            and entry.args is not None
            and tool_row.status != "removed_upstream"
        ):
            stages.append(_revalidate_args(tool_row, entry.args))
    elif entry.tool_name is None:
        stages.append({"stage": "tool", "result": "n/a", "detail": "Non-invoke action."})

    connection: Connection | None = None
    if entry.connection_id is not None:
        connection = await db.get(Connection, entry.connection_id)
    if connection is None:
        stages.append(
            {
                "stage": "connection",
                "result": "n/a",
                "detail": "No connection recorded (auth often 'none').",
            }
        )
    else:
        expires = (
            f", expires {connection.expires_at.date().isoformat()}"
            if connection.expires_at is not None
            else ""
        )
        detail = f"Connection status {connection.status}{expires}."
        if connection.status == "needs_reconnect":
            detail += " The owner must reconnect in the dashboard."
        stages.append({"stage": "connection", "result": "ok", "detail": detail})
    return stages


def _revalidate_args(tool: Tool, args: dict[str, Any]) -> dict[str, str]:
    from cam.invoke.errors import CamError
    from cam.invoke.validation import validate_args

    try:
        validate_args(tool.input_schema, args)
    except CamError as exc:
        return {"stage": "args_now", "result": "invalid", "detail": exc.message}
    except Exception:
        return {"stage": "args_now", "result": "invalid", "detail": "The stored schema is invalid."}
    return {
        "stage": "args_now",
        "result": "ok",
        "detail": "The stored args validate against the tool's current schema.",
    }
