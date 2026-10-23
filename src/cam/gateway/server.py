"""The CAM MCP server: one `cam` tool, routing instructions, per-user description.

The server is built during app startup (instructions may come from
`orgs.settings`) and mounted under `/mcp` behind `BearerAuthMiddleware`.

R26 (spec architecture.md:439): when a user's access changes, the spec wants a
`notifications/tools/list_changed` pushed to that user's open sessions. The
installed `mcp` SDK does not expose a public way to reach a live session's
`send_tool_list_changed()` from outside a request handler — the session manager
keeps transports in a private dict with no broadcast API — so that push is not
implemented here. The `cam` tool description (rebuilt on every `tools/list`) and
`list_services`/`list_tools` results already reflect access changes on the next
call. Track SDK support upstream before wiring the notification.
"""

from __future__ import annotations

import importlib
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from mcp import types as mcp_types
from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.tools import Tool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cam.access import ServiceSummary, list_services
from cam.access.skills import list_skills as access_list_skills
from cam.connections.orgs import DEFAULT_ORG_SLUG, get_org_by_slug
from cam.connections.tokens import AuthenticatedIdentity
from cam.db.models import Org
from cam.invoke.errors import internal
from cam.invoke.pipeline import AgentInfo, CamResult, GatewayDeps, error_result, handle
from cam.upstream.types import TextBlock


def _load_effective_settings() -> Any:
    """R23: load the shared effective-settings resolver if it has landed.

    Imported dynamically (not statically) so this module keeps working with the
    legacy `orgs.settings` fallback until `cam.api.settings_resolver` exists.
    TODO(r23): once the resolver is present, drop the legacy fallback below.
    """
    try:
        module = importlib.import_module("cam.api.settings_resolver")
    except ImportError:  # pragma: no cover - resolver lands in a parallel wave
        return None
    return getattr(module, "effective_settings", None)


_effective_settings: Any = _load_effective_settings()

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SkillDescription:
    id: str
    description: str
    available: bool


CAM_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "list_services",
                "list_tools",
                "describe",
                "invoke",
                "list_skills",
                "load_skill",
                "read_skill_file",
            ],
        },
        "service": {
            "type": "string",
            "description": "Service ID for list_tools, e.g. global/redash",
        },
        "id": {
            "type": "string",
            "description": "Tool ID (describe, invoke) or skill ID (load_skill, read_skill_file)",
        },
        "args": {"type": "object", "description": "Tool arguments for invoke"},
        "path": {"type": "string", "description": "Reference file path for read_skill_file"},
    },
    "required": ["action"],
}

SERVER_INSTRUCTIONS = (
    "CAM is your gateway to external services, data and skills. For any task that needs an "
    "external system (Redash, PNS, Windmill, GKE, Notion and others listed in the cam tool), "
    "use the cam tool: list_services, then list_tools, describe, invoke. Check list_skills "
    "for relevant know-how before starting a task in a listed domain."
)

DESCRIPTION_FIRST_LINE = (
    "CAM gateway to external services and skills. Use the cam tool: list_services, "
    "list_tools, describe, invoke; list_skills, load_skill, read_skill_file."
)

_DESCRIPTION_LIMIT = 2000


def build_tool_description(
    services: Sequence[ServiceSummary],
    skills: Sequence[SkillDescription] = (),
) -> str:
    service_lines = [
        f"{summary.id} ({summary.name}): {summary.description}" for summary in services
    ]
    skill_lines = [
        f"skill {skill.id}: {skill.description}"
        + (" (unavailable: requires services you cannot access)" if not skill.available else "")
        for skill in skills
    ]
    text = "\n".join([DESCRIPTION_FIRST_LINE, *service_lines, *skill_lines])
    if len(text) <= _DESCRIPTION_LIMIT:
        return text
    # Too long (many services, including templates): keep the skill descriptions
    # intact — agents rely on them — and collapse the service list to just names,
    # trimmed so the whole thing still fits.
    names = ", ".join(summary.id for summary in services)
    tail = "\n".join(skill_lines)
    overhead = len(DESCRIPTION_FIRST_LINE) + len("Available: ") + 1
    if tail:
        overhead += len(tail) + 1
    budget = _DESCRIPTION_LIMIT - overhead
    if budget < 0:
        all_names = [*(summary.id for summary in services), *(f"skill {s.id}" for s in skills)]
        compact = f"{DESCRIPTION_FIRST_LINE}\nAvailable: {', '.join(all_names)}"
        if len(compact) <= _DESCRIPTION_LIMIT:
            return compact
        return compact[: _DESCRIPTION_LIMIT - 3] + "..."
    trimmed = names[:budget]
    if len(trimmed) < len(names):
        trimmed = trimmed.rsplit(", ", 1)[0]
    prefix = f"{DESCRIPTION_FIRST_LINE}\nAvailable: {trimmed}"
    reduced = f"{prefix}\n{tail}" if tail else prefix
    if len(reduced) <= _DESCRIPTION_LIMIT:
        return reduced
    return reduced[: _DESCRIPTION_LIMIT - 3] + "..."


def _identity_from_scope(scope: Mapping[str, Any] | None) -> AuthenticatedIdentity | None:
    if scope is None:
        return None
    identity = scope.get("cam_identity")
    return identity if isinstance(identity, AuthenticatedIdentity) else None


def _request_identity(request: Any) -> AuthenticatedIdentity | None:
    return _identity_from_scope(getattr(request, "scope", None))


def _agent_info(ctx: Context[None]) -> AgentInfo:
    client_params = ctx.session.client_params
    client_info = getattr(client_params, "client_info", None)
    if client_info is not None:
        name = getattr(client_info, "name", None)
        if isinstance(name, str) and name:
            version = getattr(client_info, "version", None)
            return AgentInfo(name=name, version=version if isinstance(version, str) else None)
    headers = ctx.headers
    raw = headers.get("x-cam-agent") if headers is not None else None
    if isinstance(raw, str) and raw:
        name, slash, version = raw.partition("/")
        return AgentInfo(name=name or None, version=version if slash and version else None)
    return AgentInfo()


class ToolDescriptionMiddleware:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        result = await call_next(ctx)
        if ctx.method != "tools/list" or not isinstance(result, dict):
            return result
        tools = result.get("tools")
        if not isinstance(tools, list):
            return result
        entries = cast("list[Any]", tools)
        identity = _request_identity(ctx.request)
        if identity is None:
            return result
        try:
            async with self._session_factory() as session:
                # R26: include_template here too, otherwise connected templates
                # are missing from the per-user tool description.
                services = await list_services(session, identity, include_templates=True)
                skill_summaries = await access_list_skills(session, identity.org_id, identity.user)
        except Exception:
            logger.warning("could not build per-user cam tool description", exc_info=True)
            return result
        skill_tuple = tuple(
            SkillDescription(
                id=summary.id,
                description=summary.description,
                available=summary.available,
            )
            for summary in skill_summaries
        )
        description = build_tool_description(services, skill_tuple)
        for entry in entries:
            if isinstance(entry, dict):
                item = cast("dict[str, Any]", entry)
                if item.get("name") == "cam":
                    item["description"] = description
        return result


def to_call_tool_result(result: CamResult) -> mcp_types.CallToolResult:
    content: list[mcp_types.ContentBlock] = []
    for block in result.content:
        if isinstance(block, TextBlock):
            content.append(mcp_types.TextContent(type="text", text=block.text))
        else:
            content.append(
                mcp_types.ImageContent(type="image", data=block.data, mime_type=block.mime_type)
            )
    return mcp_types.CallToolResult(
        content=content,
        structured_content=result.structured,
        is_error=result.is_error,
    )


def _build_cam_tool(deps: GatewayDeps) -> Tool:
    async def cam(
        ctx: Context[None],
        action: Any = None,
        service: Any = None,
        id: Any = None,
        args: Any = None,
        path: Any = None,
    ) -> mcp_types.CallToolResult:
        try:
            identity = _request_identity(getattr(ctx.request_context, "request", None))
            if identity is None:
                result = error_result(internal("Request is not authenticated."))
            else:
                result = await handle(
                    deps,
                    identity,
                    _agent_info(ctx),
                    action=action,
                    service=service,
                    id=id,
                    args=args,
                    path=path,
                )
        except Exception:
            logger.exception("cam tool failed")
            result = error_result(internal())
        return to_call_tool_result(result)

    tool = Tool.from_function(
        cam,
        name="cam",
        description=DESCRIPTION_FIRST_LINE,
        context_kwarg="ctx",
        structured_output=False,
    )
    tool.parameters = CAM_TOOL_INPUT_SCHEMA
    return tool


async def load_server_instructions(
    session_factory: async_sessionmaker[AsyncSession], *, org_id: uuid.UUID | None = None
) -> str:
    """Resolve the routing instructions for an org.

    R23: prefer the shared effective-settings resolver (dashboard values, with
    defined precedence). Until it lands, fall back to the org row, accepting
    both the `server_instructions` key this gateway historically read and the
    `instructions` key the dashboard writes.
    """
    try:
        async with session_factory() as session:
            org: Org | None = None
            if org_id is not None:
                org = await session.get(Org, org_id)
            else:
                org = await get_org_by_slug(session, DEFAULT_ORG_SLUG)
            if _effective_settings is not None and org is not None:
                effective = await _effective_settings(session, org.id)
                value = getattr(effective, "server_instructions", None)
                if isinstance(value, str) and value.strip():
                    return value
            if org is not None:
                for key in ("server_instructions", "instructions"):
                    value = org.settings.get(key)
                    if isinstance(value, str) and value.strip():
                        return value
    except Exception:
        logger.warning(
            "could not load org settings; using default server instructions", exc_info=True
        )
        return SERVER_INSTRUCTIONS
    return SERVER_INSTRUCTIONS


def create_mcp_server(deps: GatewayDeps, *, instructions: str) -> MCPServer:
    tool = _build_cam_tool(deps)
    middleware: list[ServerMiddleware[Any]] = [ToolDescriptionMiddleware(deps.session_factory)]
    return MCPServer(
        name="cam",
        instructions=instructions,
        tools=[tool],
        middleware=middleware,
    )
