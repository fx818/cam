"""Shared value types for upstream MCP operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from cam.upstream.url import UrlPolicy

MessageHandler = Any  # mcp.client.session.MessageHandlerFnT; kept loose to avoid import cycles


@dataclass(frozen=True, slots=True)
class ConnectionTarget:
    """Identifies one upstream MCP endpoint for pooling purposes."""

    service_id: str
    connection_id: str
    url: str
    transport: Literal["streamable-http", "sse"] = "streamable-http"
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    message_handler: MessageHandler | None = None
    url_policy: UrlPolicy | None = None


@dataclass(frozen=True, slots=True)
class UpstreamTool:
    """A tool as advertised by an upstream MCP server."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TextBlock:
    type: Literal["text"] = "text"
    text: str = ""


@dataclass(frozen=True, slots=True)
class ImageBlock:
    type: Literal["image"] = "image"
    data: str = ""
    mime_type: str = "image/png"


ContentBlock = TextBlock | ImageBlock


@dataclass(frozen=True, slots=True)
class ToolCallOutcome:
    """Normalized `tools/call` result handed back to callers."""

    content: tuple[ContentBlock, ...]
    structured: dict[str, Any] | None
    is_error: bool
