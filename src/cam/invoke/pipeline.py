"""The cam tool action pipeline: parse, resolve, dispatch, shape the result.

The gateway stays thin: it authenticates the HTTP request, builds this
module's inputs from the MCP call, and converts a `CamResult` into an MCP
`CallToolResult`. All access checks go through `cam.access`.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, cast
from uuid import UUID

from prometheus_client import Counter
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
from cam.access.credentials import CredentialError, CredentialExpiredError, resolve_credential
from cam.api.metrics import REGISTRY, record_invoke_outcome
from cam.audit.writer import AuditEvent, AuditRef, AuditWriter
from cam.config import Settings
from cam.connections.tokens import AuthenticatedIdentity
from cam.connections.url_policy import load_url_policy
from cam.db.models import Service, Tool, User
from cam.invoke.errors import (
    CamError,
    CamErrorCode,
    internal,
    invalid_args,
    map_upstream_error,
    not_found,
    owner_deactivated,
    service_disabled,
)
from cam.invoke.rate_limit import RateLimiter
from cam.invoke.truncation import (
    chars_for_tokens,
    text_chars,
    truncate_blocks,
    truncate_structured,
    truncate_text,
)
from cam.invoke.validation import validate_args
from cam.jobs.health import record_invoke_health
from cam.skills import actions as skill_actions
from cam.upstream.errors import UpstreamError
from cam.upstream.pool import SessionPool
from cam.upstream.rest import execute_rest
from cam.upstream.types import ConnectionTarget, ContentBlock, TextBlock, ToolCallOutcome
from cam.upstream.url import UrlPolicy, url_with_credential_query

logger = logging.getLogger(__name__)

# Invoke health writes are dashboard bookkeeping; when one fails we record it
# here rather than failing the call (see `_record_invoke_health_safe`).
HEALTH_WRITE_FAILED = Counter(
    "cam_invoke_health_write_failed_total",
    "Invoke health updates that could not be persisted (dashboard badge may be stale)",
    registry=REGISTRY,
)

# Audit events the writer refused to enqueue (queue full or writer closed). The
# writer already counts queue-full drops; this counts the pipeline-level misses
# it reports back through `AuditWriter.record`'s return value.
AUDIT_RECORD_REJECTED = Counter(
    "cam_invoke_audit_record_rejected_total",
    "Invoke audit events the writer refused to enqueue (queue full or writer closed)",
    registry=REGISTRY,
)

# Slack kept aside so a capped error envelope (code/service/message plus JSON
# punctuation) still fits under the response budget after its message is cut.
_ERROR_ENVELOPE_OVERHEAD = 256

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


MessageHandlerFactory = Callable[[str, str], Any]


@dataclass(frozen=True, slots=True)
class GatewayDeps:
    session_factory: async_sessionmaker[AsyncSession]
    pool: SessionPool
    settings: Settings
    rate_limiter: RateLimiter
    audit: AuditWriter
    message_handler_factory: MessageHandlerFactory | None = None


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


async def closest_tool_hint(
    deps: GatewayDeps, identity: AuthenticatedIdentity, raw_id: str
) -> str | None:
    """Suggest the closest tool ID the caller can actually reach.

    Candidates come strictly from the caller's access-filtered list, so the
    hint never reveals services the caller cannot see (NOT_FOUND stays opaque).
    """
    import difflib

    try:
        async with deps.session_factory() as session:
            summaries = await list_services(session, identity, include_templates=True)
            candidates: list[str] = []
            for summary in summaries:
                resolved_service, tools = await list_tools(session, identity, summary.id)
                candidates.extend(
                    tool_ref(resolved_service.service, tool, resolved_service.owner_handle)
                    for tool in tools
                )
    except Exception:
        logger.debug("could not build NOT_FOUND suggestion", exc_info=True)
        return None
    matches = difflib.get_close_matches(raw_id, candidates, n=1, cutoff=0.4)
    return matches[0] if matches else None


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
                "service_id": summary.service_id,
                "name": summary.name,
                "description": summary.description,
                "tier": summary.tier,
                "kind": summary.kind,
                "owner": summary.owner,
                "shared_by": summary.shared_by,
                "health": summary.health,
                "tool_count": summary.tool_count,
                "connected": summary.connected,
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
        summaries = await list_services(session, identity, include_templates=True)
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
    tool = resolved.tool
    payload: dict[str, Any] = {
        "id": resolved.id,
        "description": _tool_description(tool),
        "input_schema": tool.input_schema or {},
    }
    if tool.usage_note:
        payload["usage_note"] = tool.usage_note
    example = tool.example_invocation if isinstance(tool.example_invocation, dict) else None
    if example:
        payload["example"] = example
    return payload_result(
        payload,
        AuditRef(service_id=resolved.service.id, tool_name=tool.name),
    )


async def _record_invoke_health_safe(
    deps: GatewayDeps,
    *,
    service_id: UUID,
    connection_id: UUID,
    ok: bool,
    error_code: str | None,
) -> None:
    """Record invoke-driven health without ever failing the call.

    A health write only refreshes a dashboard badge. A database error here must
    not turn a dispatch that already happened into a retryable ``INTERNAL`` error
    — the agent would retry and duplicate the upstream side effect. Failures are
    logged and counted instead; the real upstream outcome is preserved.
    """
    try:
        await record_invoke_health(
            deps.session_factory,
            service_id=service_id,
            connection_id=connection_id,
            ok=ok,
            error_code=error_code,
        )
    except Exception:
        HEALTH_WRITE_FAILED.inc()
        logger.warning(
            "could not record invoke health for service %s (connection %s)",
            service_id,
            connection_id,
            exc_info=True,
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
        connection = await resolve_connection(
            session,
            resolved.service,
            ref=resolved.service_ref,
            user_id=identity.user.id,
        )
        audit_ref = AuditRef(
            service_id=resolved.service.id,
            connection_id=connection.id,
            tool_name=resolved.tool.name,
            owner_user_id=connection.user_id or resolved.service.owner_user_id,
        )
        try:
            credential = await resolve_credential(
                session, service=resolved.service, connection=connection
            )
        except CredentialExpiredError as exc:
            owner_id = connection.user_id or resolved.service.owner_user_id
            owner_name = None
            if owner_id is not None:
                owner = await session.get(User, owner_id)
                if owner is not None:
                    owner_name = owner.display_name
            if owner_name:
                message = (
                    f"{owner_name}'s {resolved.service.name} connection has expired. "
                    f"{owner_name} needs to reconnect it in the CAM dashboard."
                )
            else:
                message = (
                    f"The {resolved.service.name} connection has expired. "
                    "An admin needs to reconnect it in the CAM dashboard."
                )
            await _record_invoke_health_safe(
                deps,
                service_id=resolved.service.id,
                connection_id=connection.id,
                ok=False,
                error_code="CONNECTION_EXPIRED",
            )
            raise CamError(
                CamErrorCode.CONNECTION_EXPIRED,
                message,
                service=resolved.service_ref,
                audit_ref=audit_ref,
            ) from exc
        except CredentialError as exc:
            await _record_invoke_health_safe(
                deps,
                service_id=resolved.service.id,
                connection_id=connection.id,
                ok=False,
                error_code="UPSTREAM_AUTH",
            )
            raise CamError(
                CamErrorCode.UPSTREAM_AUTH,
                "Credential rejected. Owner needs to reconnect.",
                service=resolved.service_ref,
                audit_ref=audit_ref,
            ) from exc
        url_policy = await load_url_policy(
            session,
            org_id=resolved.service.org_id,
            restricted=resolved.service.tier == "personal",
        )
        try:
            validate_args(resolved.tool.input_schema, call_args)
        except CamError as exc:
            exc.audit_ref = audit_ref
            raise

    service = resolved.service
    ref = resolved.service_ref
    try:
        if service.kind == "rest":
            outcome = await execute_rest(
                base_url=service.url,
                binding=resolved.tool.rest_binding or {},
                args=call_args,
                headers=credential.headers,
                query=credential.query,
                timeout_ms=service.timeout_ms,
                url_policy=url_policy,
            )
        else:
            outcome = await _dispatch_mcp(
                deps,
                service=service,
                connection_id=connection.id,
                tool=resolved.tool,
                call_args=call_args,
                credential_headers=dict(credential.headers),
                credential_query=dict(credential.query),
                url_policy=url_policy,
            )
    except UpstreamError as exc:
        cam_error = map_upstream_error(
            exc, service_ref=ref, service_name=service.name, audit_ref=audit_ref
        )
        await _record_invoke_health_safe(
            deps,
            service_id=service.id,
            connection_id=connection.id,
            ok=False,
            error_code=str(cam_error.code),
        )
        raise cam_error from exc
    if outcome.is_error:
        upstream_text = "".join(
            block.text for block in outcome.content if isinstance(block, TextBlock)
        )
        await _record_invoke_health_safe(
            deps,
            service_id=service.id,
            connection_id=connection.id,
            ok=False,
            error_code="UPSTREAM_ERROR",
        )
        cap_tokens = service.response_cap_tokens
        error_budget = max(chars_for_tokens(cap_tokens) - _ERROR_ENVELOPE_OVERHEAD, 0)
        upstream_text, _ = truncate_text(
            upstream_text, max_chars=error_budget, cap_tokens=cap_tokens
        )
        raise CamError(
            CamErrorCode.UPSTREAM_ERROR,
            upstream_text or f"{service.name} returned an error.",
            service=ref,
            audit_ref=audit_ref,
        )
    await _record_invoke_health_safe(
        deps,
        service_id=service.id,
        connection_id=connection.id,
        ok=True,
        error_code=None,
    )
    cap_tokens = service.response_cap_tokens
    content, truncated = truncate_blocks(outcome.content, cap_tokens)
    structured, structured_truncated = truncate_structured(
        outcome.structured,
        cap_tokens=cap_tokens,
        max_chars=max(chars_for_tokens(cap_tokens) - text_chars(content), 0),
    )
    return CamResult(
        content=content,
        structured=structured,
        is_error=False,
        truncated=truncated or structured_truncated,
        audit_ref=audit_ref,
    )


async def _dispatch_mcp(
    deps: GatewayDeps,
    *,
    service: Service,
    connection_id: UUID,
    tool: Tool,
    call_args: dict[str, Any],
    credential_headers: dict[str, str],
    credential_query: dict[str, str],
    url_policy: UrlPolicy,
) -> ToolCallOutcome:
    transport = (
        service.transport if service.transport in ("streamable-http", "sse") else "streamable-http"
    )
    url = url_with_credential_query(service.url, credential_query)
    target = ConnectionTarget(
        service_id=str(service.id),
        connection_id=str(connection_id),
        url=url,
        transport=transport,
        headers=credential_headers,
        url_policy=url_policy,
    )
    if deps.message_handler_factory is not None:
        target = replace(
            target,
            message_handler=deps.message_handler_factory(str(service.id), str(connection_id)),
        )
    upstream = await deps.pool.session(target)
    return await upstream.call_tool(
        tool.upstream_name, call_args, timeout_s=service.timeout_ms / 1000
    )


async def _dispatch(
    deps: GatewayDeps,
    identity: AuthenticatedIdentity,
    *,
    action: Any,
    service: Any,
    tool_id: Any,
    args: Any,
    path: Any,
) -> CamResult | dict[str, Any]:
    if action is None:
        raise invalid_args("action", "is required")
    if not isinstance(action, str):
        raise invalid_args("action", "must be a string")
    if action not in CAM_ACTIONS:
        valid = ", ".join(sorted(CAM_ACTIONS))
        raise invalid_args("action", f"unknown action {action!r}; valid: {valid}")
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
    payload, audit_ref = await _skill_actions(
        deps,
        identity,
        action=action,
        skill_id=tool_id,
        path=path,
    )
    return payload_result(payload, audit_ref)


async def _skill_actions(
    deps: GatewayDeps,
    identity: AuthenticatedIdentity,
    *,
    action: str,
    skill_id: Any,
    path: Any,
) -> tuple[dict[str, Any], AuditRef | None]:
    if action == "list_skills":
        payload = await skill_actions.list_skills_payload(deps.session_factory, identity)
        return payload, None
    async with deps.session_factory() as session:
        if action == "load_skill":
            return await skill_actions.load_skill_payload(
                session, identity.org_id, identity.user, skill_id
            )
        return await skill_actions.read_skill_file_payload(
            session, identity.org_id, identity.user, skill_id, path
        )


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
            path=path,
        )
    except CamError as exc:
        cam_error = exc
        result = error_result(exc)
    except ServiceNotFound:
        cam_error = not_found()
        if action in ("invoke", "describe") and isinstance(id, str) and id:
            suggestion = await closest_tool_hint(deps, identity, id)
            if suggestion:
                cam_error = CamError(
                    CamErrorCode.NOT_FOUND,
                    f'Tool not found or not available. Closest match: "{suggestion}".',
                )
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

    # Error envelopes bypass the invoke-path cap, and an upstream error message
    # can be unbounded. Cap every error result under the default budget so no
    # response leaves CAM larger than the configured cap.
    if result.is_error and not result.truncated:
        capped_content, cut = truncate_blocks(
            result.content, deps.settings.default_response_cap_tokens
        )
        if cut:
            result = replace(result, content=capped_content, truncated=True)

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
    if not deps.audit.record(event):
        # The writer refused the event (queue full or writer closed). The call
        # itself succeeded; surface the loss instead of silently dropping it.
        AUDIT_RECORD_REJECTED.inc()
        logger.warning(
            "audit event not recorded (writer closed or queue full): action=%s status=%s",
            event.action,
            event.status,
        )
    # R28: expose tool-level outcomes at the pipeline boundary — the MCP call
    # itself returns HTTP 200 even for failures, so HTTP-status alerts miss them.
    record_invoke_outcome(
        event.action,
        is_error=result.is_error,
        error_code=str(cam_error.code) if cam_error is not None else None,
    )
    return result
