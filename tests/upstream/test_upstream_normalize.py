"""Unit tests for content-block normalization."""

from mcp import types as mcp_types

from cam.upstream.normalize import MAX_IMAGES, normalize_call_result
from cam.upstream.types import ImageBlock, TextBlock

_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQ"
    "GAhKmMIQAAAABJRU5ErkJggg=="
)


def _text(text: str) -> mcp_types.TextContent:
    return mcp_types.TextContent(type="text", text=text)


def _image(data: str = _PNG) -> mcp_types.ImageContent:
    return mcp_types.ImageContent(type="image", data=data, mime_type="image/png")


def test_text_passthrough() -> None:
    result = mcp_types.CallToolResult(content=[_text("hello")])
    outcome = normalize_call_result(result)
    assert outcome.is_error is False
    assert outcome.structured is None
    assert outcome.content == (TextBlock(text="hello"),)


def test_structured_content_passthrough() -> None:
    result = mcp_types.CallToolResult(
        content=[_text('{"a":1}')],
        structured_content={"a": 1},
    )
    outcome = normalize_call_result(result)
    assert outcome.structured == {"a": 1}


def test_image_passthrough() -> None:
    result = mcp_types.CallToolResult(content=[_image()])
    outcome = normalize_call_result(result)
    assert outcome.content == (ImageBlock(data=_PNG, mime_type="image/png"),)


def test_is_error_preserved() -> None:
    result = mcp_types.CallToolResult(content=[_text("nope")], is_error=True)
    outcome = normalize_call_result(result)
    assert outcome.is_error is True


def test_audio_becomes_note() -> None:
    audio = mcp_types.AudioContent(type="audio", data="AAAA", mime_type="audio/wav")
    outcome = normalize_call_result(mcp_types.CallToolResult(content=[audio]))
    assert len(outcome.content) == 1
    block = outcome.content[0]
    assert isinstance(block, TextBlock)
    assert "not supported" in block.text


def test_resource_link_becomes_note() -> None:
    link = mcp_types.ResourceLink(type="resource_link", name="doc", uri="file://x")
    outcome = normalize_call_result(mcp_types.CallToolResult(content=[link]))
    block = outcome.content[0]
    assert isinstance(block, TextBlock)
    assert "not supported" in block.text


def test_embedded_resource_becomes_note() -> None:
    resource = mcp_types.EmbeddedResource(
        type="resource",
        resource=mcp_types.TextResourceContents(uri="file://x", text="body"),
    )
    outcome = normalize_call_result(mcp_types.CallToolResult(content=[resource]))
    block = outcome.content[0]
    assert isinstance(block, TextBlock)
    assert "not supported" in block.text


def test_image_count_cap() -> None:
    content: list[mcp_types.ContentBlock] = [_image() for _ in range(MAX_IMAGES + 2)]
    content.insert(0, _text("intro"))
    outcome = normalize_call_result(mcp_types.CallToolResult(content=content))
    images = [b for b in outcome.content if isinstance(b, ImageBlock)]
    notes = [b for b in outcome.content if isinstance(b, TextBlock) and "image omitted" in b.text]
    assert len(images) == MAX_IMAGES
    assert len(notes) == 2


def test_oversized_image_becomes_note() -> None:
    # 6 MB of 'A' base64-encoded exceeds the 5 MB decoded cap.
    oversized = "A" * (7 * 1024 * 1024)
    outcome = normalize_call_result(mcp_types.CallToolResult(content=[_image(oversized)]))
    block = outcome.content[0]
    assert isinstance(block, TextBlock)
    assert "image omitted" in block.text


def test_empty_result() -> None:
    outcome = normalize_call_result(mcp_types.CallToolResult(content=[]))
    assert outcome.content == ()
    assert outcome.structured is None
    assert outcome.is_error is False
