"""Normalize MCP `tools/call` results into the CAM content shape.

Launch-scope rules (docs/architecture.md → Implementation spec → Launch scope):
- Pass text and images through.
- At most 5 images per response and 5 MB each; anything beyond becomes a note.
- Audio, resource links, embedded resources and unknown block types become a
  one-line text note. Images never count toward the token cap (that is applied
  later by the gateway, not here).
"""

from __future__ import annotations

from typing import Any

from mcp import types as mcp_types

from cam.upstream.types import ContentBlock, ImageBlock, TextBlock, ToolCallOutcome

MAX_IMAGES = 5
MAX_IMAGE_BYTES = 5 * 1024 * 1024

_IMAGE_NOTE = "[CAM: image omitted — response allows at most 5 images of 5 MB each]"


def _decoded_b64_len(data: str) -> int:
    padding = 0
    if data.endswith("=="):
        padding = 2
    elif data.endswith("="):
        padding = 1
    return (len(data) * 3) // 4 - padding


def _note(kind: str) -> TextBlock:
    return TextBlock(text=f"[CAM: {kind} content not supported; omitted]")


def _block(block: object, image_count: int) -> tuple[ContentBlock, int]:
    if isinstance(block, mcp_types.TextContent):
        return TextBlock(text=block.text), image_count
    if isinstance(block, mcp_types.ImageContent):
        if image_count >= MAX_IMAGES or _decoded_b64_len(block.data) > MAX_IMAGE_BYTES:
            return TextBlock(text=_IMAGE_NOTE), image_count
        return ImageBlock(data=block.data, mime_type=block.mime_type), image_count + 1
    if isinstance(block, mcp_types.AudioContent):
        return _note("audio"), image_count
    if isinstance(block, mcp_types.ResourceLink):
        return _note("resource link"), image_count
    if isinstance(block, mcp_types.EmbeddedResource):
        return _note("resource"), image_count
    return _note("unknown"), image_count


def normalize_call_result(result: mcp_types.CallToolResult) -> ToolCallOutcome:
    """Convert an MCP `CallToolResult` into a `ToolCallOutcome`."""
    content: list[ContentBlock] = []
    image_count = 0
    for raw in result.content:
        block, image_count = _block(raw, image_count)
        content.append(block)
    raw_structured: dict[str, Any] | None = result.structured_content
    structured = dict(raw_structured) if raw_structured is not None else None
    return ToolCallOutcome(
        content=tuple(content),
        structured=structured,
        is_error=bool(result.is_error),
    )
