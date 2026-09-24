"""Response-size cap: estimate tokens as characters/4 and truncate with a note.

A JSON array at the top level keeps whole leading items that fit; other text
cuts at the last newline before the limit, falling back to a hard cut.
"""

from __future__ import annotations

import json
from typing import Any, cast

from cam.upstream.types import ContentBlock, TextBlock

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def chars_for_tokens(cap_tokens: int) -> int:
    return cap_tokens * CHARS_PER_TOKEN


def _array_note(kept: int, total: int, cap_tokens: int) -> str:
    return (
        f"[CAM: response truncated — showing {kept} of {total} items "
        f"(~{cap_tokens} token cap). Narrow the request with filters or a limit.]"
    )


def _text_note(cap_tokens: int) -> str:
    return (
        f"[CAM: response truncated — showing the first ~{cap_tokens} tokens. "
        "Narrow the request with filters or a limit.]"
    )


def _truncate_json_array(text: str, *, max_chars: int, cap_tokens: int) -> tuple[str, bool] | None:
    try:
        data: Any = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    items = cast("list[Any]", data)
    total = len(items)
    kept: list[str] = []
    for item in items:
        item_text = json.dumps(item, ensure_ascii=False)
        candidate = [*kept, item_text]
        head = "[" + ",".join(candidate) + "]"
        note = _array_note(len(candidate), total, cap_tokens)
        if len(head) + 1 + len(note) > max_chars:
            break
        kept = candidate
    if not kept:
        return None
    head = "[" + ",".join(kept) + "]"
    note = _array_note(len(kept), total, cap_tokens)
    return f"{head}\n{note}", True


def truncate_text(text: str, *, max_chars: int, cap_tokens: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    if max_chars <= 0:
        return _text_note(cap_tokens), True
    stripped = text.lstrip()
    if stripped.startswith("["):
        array_result = _truncate_json_array(stripped, max_chars=max_chars, cap_tokens=cap_tokens)
        if array_result is not None:
            return array_result
    cut = text.rfind("\n", 0, max_chars)
    if cut <= 0:
        cut = max_chars
    head = text[:cut].rstrip("\n")
    note = _text_note(cap_tokens)
    if len(head) + 1 + len(note) > max_chars:
        head = text[: max(max_chars - len(note) - 1, 0)].rstrip("\n")
    return f"{head}\n{note}", True


def truncate_blocks(
    content: tuple[ContentBlock, ...], cap_tokens: int
) -> tuple[tuple[ContentBlock, ...], bool]:
    if cap_tokens < 1:
        return content, False
    budget = chars_for_tokens(cap_tokens)
    truncated = False
    blocks: list[ContentBlock] = []
    for block in content:
        if isinstance(block, TextBlock):
            new_text, cut = truncate_text(block.text, max_chars=budget, cap_tokens=cap_tokens)
            truncated = truncated or cut
            budget -= len(new_text)
            blocks.append(TextBlock(text=new_text) if cut else block)
        else:
            blocks.append(block)
    return tuple(blocks), truncated
