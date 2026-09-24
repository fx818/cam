"""The cam tool action pipeline: parse, resolve, dispatch, shape the result.

The gateway stays thin: it authenticates the HTTP request, builds this
module's inputs from the MCP call, and converts a `CamResult` into an MCP
`CallToolResult`. All access checks go through `cam.access`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cam.access import (
    ResolvedTool,
    ServiceDisabled,
    ServiceNotFound,
    ServiceSummary,
    list_services,
    list_tools,
    resolve_org_connection,
    resolve_tool,
)
from cam.config import Settings
from cam.connections.tokens import AuthenticatedIdentity
from cam.db.models import Tool
from cam.invoke.errors import (
    CamError,
    CamErrorCode,
    internal,
    invalid_args,
    map_upstream_error,
    not_found,
    service_disabled,
    skills_unavailable,
)
from cam.upstream.errors import UpstreamError
from cam.upstream.pool import SessionPool
from cam.upstream.types import ConnectionTarget, ContentBlock, TextBlock

logger = logging.getLogger(__name__)

SKILL_ACTIONS = frozenset({"list_skills", "load_skill", "read_skill_file"})
CAM_ACTIONS = frozenset({"list_services", "list_tools", "describe", "invoke", *SKILL_ACTIONS})


@dataclass(frozen=True, slots=True)
class AgentInfo:
    name: str | None = None
    version: str | None = None


@dataclass(frozen=True, slots=True)
class CamResult:
    content: tuple[ContentBlock, ...] = ()
    structured: dict[str, Any] | None = None
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class GatewayDeps:
    session_factory: async_sessionmaker[AsyncSession]
    pool: SessionPool
    settings: Settings


def error_result(exc: CamError) -> CamResult:
    return CamResult(content=(TextBlock(text=exc.as_json()),), structured=None, is_error=True)


def payload_result(payload: dict[str, Any]) -> CamResult:
    return CamResult(
        content=(TextBlock(text=json.dumps(payload)),),
        structured=payload,
        is_error=False,
    )


def _require_str(value: Any, field_name: str) -> str:
    if value is None:
        raise invalid_args(field_name, "is required")
    if not isinstance(value, str):
        raise invalid_args(field_name, "must be a string")
    return value


def _service_summary_payload(summaries: list[ServiceSummary]) -> dict[str, Any]:
    return {
        "services": [
            {
                "id": summary.id,
                "name": summary.name,
                "description": summary.description,
                "tier": summary.tier,
                "owner": summary.owner,
                "shared_by": summary.shared_by,
                "health": summary.health,
                "tool_count": summary.tool_count,
            }
            for summary in summaries
        ]
    }


def _tool_description(tool: Tool) -> str:
    override = getattr(tool, "description_override", None)
    if override:
        return override
    return tool.description or ""


async def _list_services(deps: GatewayDeps, identity: AuthenticatedIdentity) -> dict[str, Any]:
    async with deps.session_factory() as session:
        summaries = await list_services(session, identity)
    return _service_summary_payload(summaries)


async def _list_tools(
    deps: GatewayDeps, identity: AuthenticatedIdentity, service: Any
) -> dict[str, Any]:
    ref = _require_str(service, "service")
    async with deps.session_factory() as session:
        resolved_service, tools = await list_tools(session, identity, ref)
    return {
        "service": f"global/{resolved_service.slug}",
        "tools": [
            {
                "id": f"global/{resolved_service.slug}/{tool.name}",
                "description": _tool_description(tool),
            }
            for tool in tools
        ],
    }


async def _describe(
    deps: GatewayDeps, identity: AuthenticatedIdentity, tool_id: Any
) -> dict[str, Any]:
    raw_id = _require_str(tool_id, "id")
    async with deps.session_factory() as session:
        resolved = await resolve_tool(session, identity, raw_id)
    return {
        "id": resolved.id,
        "description": _tool_description(resolved.tool),
        "input_schema": resolved.tool.input_schema or {},
    }


async def _invoke(
    deps: GatewayDeps,
    identity: AuthenticatedIdentity,
    tool_id: Any,
    args: Any,
) -> CamResult:
    raw_id = _require_str(tool_id, "id")
    if args is None:
        call_args: dict[str, Any] = {}
    elif isinstance(args, dict):
        call_args = cast("dict[str, Any]", args)
    else:
        raise invalid_args("args", "must be an object")

    async with deps.session_factory() as session:
        resolved: ResolvedTool = await resolve_tool(session, identity, raw_id)
        connection = await resolve_org_connection(session, resolved.service)

    service = resolved.service
    ref = f"global/{service.slug}"
    transport = (
        service.transport if service.transport in ("streamable-http", "sse") else "streamable-http"
    )
    target = ConnectionTarget(
        service_id=str(service.id),
        connection_id=str(connection.id),
        url=service.url,
        transport=transport,
        headers={},
    )
    upstream = await deps.pool.session(target)
    try:
        outcome = await upstream.call_tool(resolved.tool.upstream_name, call_args)
    except UpstreamError as exc:
        raise map_upstream_error(exc, service_ref=ref, service_name=service.name) from exc
    if outcome.is_error:
        upstream_text = "".join(
            block.text for block in outcome.content if isinstance(block, TextBlock)
        )
        raise CamError(
            CamErrorCode.UPSTREAM_ERROR,
            upstream_text or f"{service.name} returned an error.",
            service=ref,
        )
    return CamResult(content=outcome.content, structured=outcome.structured, is_error=False)


async def _dispatch(
    deps: GatewayDeps,
    identity: AuthenticatedIdentity,
    *,
    action: Any,
    service: Any,
    tool_id: Any,
    args: Any,
) -> CamResult | dict[str, Any]:
    if action is None:
        raise invalid_args("action", "is required")
    if not isinstance(action, str):
        raise invalid_args("action", "must be a string")
    if action not in CAM_ACTIONS:
        raise invalid_args("action", f"unknown action {action!r}")
    if action == "list_services":
        return await _list_services(deps, identity)
    if action == "list_tools":
        return await _list_tools(deps, identity, service)
    if action == "describe":
        return await _describe(deps, identity, tool_id)
    if action == "invoke":
        return await _invoke(deps, identity, tool_id, args)
    raise skills_unavailable()


async def handle(
    deps: GatewayDeps,
    identity: AuthenticatedIdentity,
    agent: AgentInfo,
    *,
    action: Any = None,
    service: Any = None,
    id: Any = None,
    args: Any = None,
    path: Any = None,
) -> CamResult:
    del agent, path
    try:
        outcome = await _dispatch(
            deps,
            identity,
            action=action,
            service=service,
            tool_id=id,
            args=args,
        )
    except CamError as exc:
        return error_result(exc)
    except ServiceNotFound:
        return error_result(not_found())
    except ServiceDisabled as exc:
        return error_result(service_disabled(str(exc) or None))
    except Exception:
        logger.exception("cam action failed")
        return error_result(internal())
    if isinstance(outcome, CamResult):
        return outcome
    return payload_result(outcome)
