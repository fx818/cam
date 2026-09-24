"""The cam tool action pipeline: parse, resolve, dispatch, shape the result.

The gateway stays thin: it authenticates the HTTP request, builds this
module's inputs from the MCP call, and converts a `CamResult` into an MCP
`CallToolResult`. All access checks go through `cam.access`.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cam.access import (
    OwnerDeactivated,
    ResolvedTool,
    ServiceDisabled,
    ServiceNotFound,
    ServiceSummary,
    list_services,
    list_tools,
    resolve_connection,
    resolve_tool,
    tool_ref,
)
from cam.access.credentials import CredentialError, resolve_credential_headers
from cam.audit.writer import AuditEvent, AuditRef, AuditWriter
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
    owner_deactivated,
    service_disabled,
    skills_unavailable,
)
from cam.invoke.rate_limit import RateLimiter
from cam.invoke.truncation import truncate_blocks
from cam.invoke.validation import validate_args
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
    truncated: bool = False
    audit_ref: AuditRef | None = None


@dataclass(frozen=True, slots=True)
class GatewayDeps:
    session_factory: async_sessionmaker[AsyncSession]
    pool: SessionPool
    settings: Settings
    rate_limiter: RateLimiter
    audit: AuditWriter


def error_result(exc: CamError) -> CamResult:
    return CamResult(
        content=(TextBlock(text=exc.as_json()),),
        structured=None,
        is_error=True,
        audit_ref=exc.audit_ref,
    )


def payload_result(payload: dict[str, Any], audit_ref: AuditRef | None = None) -> CamResult:
    return CamResult(
        content=(TextBlock(text=json.dumps(payload)),),
        structured=payload,
        is_error=False,
        audit_ref=audit_ref,
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
) -> CamResult:
    ref = _require_str(service, "service")
    async with deps.session_factory() as session:
        resolved_service, tools = await list_tools(session, identity, ref)
    payload = {
        "service": resolved_service.ref,
        "tools": [
            {
                "id": tool_ref(resolved_service.service, tool, resolved_service.owner_handle),
                "description": _tool_description(tool),
            }
            for tool in tools
        ],
    }
    return payload_result(payload, AuditRef(service_id=resolved_service.service.id))


async def _describe(deps: GatewayDeps, identity: AuthenticatedIdentity, tool_id: Any) -> CamResult:
    raw_id = _require_str(tool_id, "id")
    async with deps.session_factory() as session:
        resolved = await resolve_tool(session, identity, raw_id)
    payload = {
        "id": resolved.id,
        "description": _tool_description(resolved.tool),
        "input_schema": resolved.tool.input_schema or {},
    }
    return payload_result(
        payload,
        AuditRef(service_id=resolved.service.id, tool_name=resolved.tool.name),
    )


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
        connection = await resolve_connection(session, resolved.service, ref=resolved.service_ref)
        audit_ref = AuditRef(
            service_id=resolved.service.id,
            connection_id=connection.id,
            tool_name=resolved.tool.name,
        )
        try:
            headers = await resolve_credential_headers(
                session, service=resolved.service, connection=connection
            )
        except CredentialError as exc:
            raise CamError(
                CamErrorCode.UPSTREAM_AUTH,
                "Credential rejected. Owner needs to reconnect.",
                service=resolved.service_ref,
                audit_ref=audit_ref,
            ) from exc
        try:
            validate_args(resolved.tool.input_schema, call_args)
        except CamError as exc:
            exc.audit_ref = audit_ref
            raise

    service = resolved.service
    ref = resolved.service_ref
    transport = (
        service.transport if service.transport in ("streamable-http", "sse") else "streamable-http"
    )
    target = ConnectionTarget(
        service_id=str(service.id),
        connection_id=str(connection.id),
        url=service.url,
        transport=transport,
        headers=headers,
    )
    try:
        upstream = await deps.pool.session(target)
        outcome = await upstream.call_tool(
            resolved.tool.upstream_name, call_args, timeout_s=service.timeout_ms / 1000
        )
    except UpstreamError as exc:
        raise map_upstream_error(
            exc, service_ref=ref, service_name=service.name, audit_ref=audit_ref
        ) from exc
    if outcome.is_error:
        upstream_text = "".join(
            block.text for block in outcome.content if isinstance(block, TextBlock)
        )
        raise CamError(
            CamErrorCode.UPSTREAM_ERROR,
            upstream_text or f"{service.name} returned an error.",
            service=ref,
            audit_ref=audit_ref,
        )
    content, truncated = truncate_blocks(outcome.content, service.response_cap_tokens)
    return CamResult(
        content=content,
        structured=outcome.structured,
        is_error=False,
        truncated=truncated,
        audit_ref=audit_ref,
    )


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
        if not deps.rate_limiter.allow(identity.token.id):
            limit = deps.rate_limiter.limit
            raise CamError(
                CamErrorCode.CAM_RATE_LIMITED,
                f"Rate limit exceeded: at most {limit} invoke calls per minute "
                "per token. Retry later.",
            )
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
    del path
    started = time.monotonic()
    captured_args: dict[str, Any] | None = None
    if action == "invoke":
        if args is None:
            captured_args = {}
        elif isinstance(args, dict):
            captured_args = dict(cast("dict[str, Any]", args))
    cam_error: CamError | None = None
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
        cam_error = exc
        result = error_result(exc)
    except ServiceNotFound:
        cam_error = not_found()
        result = error_result(cam_error)
    except ServiceDisabled as exc:
        cam_error = service_disabled(str(exc) or None)
        cam_error.audit_ref = exc.audit_ref
        result = error_result(cam_error)
    except OwnerDeactivated as exc:
        cam_error = owner_deactivated(str(exc) or None)
        cam_error.audit_ref = exc.audit_ref
        result = error_result(cam_error)
    except Exception:
        logger.exception("cam action failed")
        cam_error = internal()
        result = error_result(cam_error)
    else:
        result = outcome if isinstance(outcome, CamResult) else payload_result(outcome)

    latency_ms = int((time.monotonic() - started) * 1000)
    response_bytes = sum(
        len(block.text.encode("utf-8")) if isinstance(block, TextBlock) else len(block.data)
        for block in result.content
    )
    audit_ref = result.audit_ref
    event = AuditEvent(
        org_id=identity.org_id,
        user_id=identity.user.id,
        token_id=identity.token.id,
        agent_name=agent.name,
        agent_version=agent.version,
        action=action if isinstance(action, str) else "unknown",
        args=captured_args,
        status="error" if result.is_error else "ok",
        error_code=str(cam_error.code) if cam_error is not None else None,
        error_message=cam_error.message if cam_error is not None else None,
        latency_ms=latency_ms,
        response_bytes=response_bytes,
        truncated=result.truncated,
    ).from_ref(audit_ref)
    deps.audit.record(event)
    return result
