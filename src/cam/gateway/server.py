"""The CAM MCP server: one `cam` tool, routing instructions, per-user description.

The server is built during app startup (instructions may come from
`orgs.settings`) and mounted under `/mcp` behind `BearerAuthMiddleware`.
"""

from __future__ import annotations

import logging
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
from cam.invoke.errors import internal
from cam.invoke.pipeline import AgentInfo, CamResult, GatewayDeps, error_result, handle
from cam.upstream.types import TextBlock

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
    lines = [DESCRIPTION_FIRST_LINE]
    lines.extend(f"{summary.id} ({summary.name}): {summary.description}" for summary in services)
    lines.extend(
        f"skill {skill.id}: {skill.description}"
        + (" (unavailable: requires services you cannot access)" if not skill.available else "")
        for skill in skills
    )
    text = "\n".join(lines)
    if len(text) <= _DESCRIPTION_LIMIT:
        return text
    names = [summary.id for summary in services]
    names.extend(f"skill {skill.id}" for skill in skills)
    compact = f"{DESCRIPTION_FIRST_LINE}\nAvailable: {', '.join(names)}"
    if len(compact) <= _DESCRIPTION_LIMIT:
        return compact
    return compact[: _DESCRIPTION_LIMIT - 3] + "..."


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
                services = await list_services(session, identity)
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


async def load_server_instructions(session_factory: async_sessionmaker[AsyncSession]) -> str:
    try:
        async with session_factory() as session:
            org = await get_org_by_slug(session, DEFAULT_ORG_SLUG)
    except Exception:
        logger.warning(
            "could not load org settings; using default server instructions", exc_info=True
        )
        return SERVER_INSTRUCTIONS
    if org is None:
        return SERVER_INSTRUCTIONS
    value = org.settings.get("server_instructions")
    if isinstance(value, str) and value.strip():
        return value
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
