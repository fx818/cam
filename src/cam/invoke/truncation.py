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


def _dict_note(kept: int, total: int, cap_tokens: int) -> str:
    return (
        f"[CAM: response truncated — showing {kept} of {total} fields "
        f"(~{cap_tokens} token cap). Narrow the request with filters or a limit.]"
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


def text_chars(content: tuple[ContentBlock, ...]) -> int:
    """Characters of *text* (not images) in a block tuple, for budget sharing."""
    return sum(len(block.text) for block in content if isinstance(block, TextBlock))


def truncate_structured(
    value: Any, *, cap_tokens: int, max_chars: int | None = None
) -> tuple[Any, bool]:
    """Cap a structured (JSON) result under the response budget.

    Returns ``(value, truncated)``. A value that already fits is returned
    unchanged. A top-level list keeps whole leading items and a top-level object
    keeps whole leading keys, so a truncated result stays valid JSON that matches
    the truncated text blocks. When not even one element fits, a small marker
    object (or ``None`` when there is no budget left) is returned so the field is
    still valid, bounded JSON rather than raw uncapped data.

    ``max_chars`` lets a caller share one budget with the text blocks: pass the
    characters still available after ``truncate_blocks`` ran.
    """
    if value is None or cap_tokens < 1:
        return value, False
    budget = chars_for_tokens(cap_tokens) if max_chars is None else max_chars
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError, ValueError:
        return value, False
    if len(encoded) <= budget:
        return value, False
    if budget <= 0:
        return None, True
    if isinstance(value, list):
        items = cast("list[Any]", value)
        kept: list[Any] = []
        head_len = 2  # "[" + "]"
        for item in items:
            item_len = len(json.dumps(item, ensure_ascii=False, default=str))
            # json's default item separator is ", " (2 chars).
            candidate_len = head_len + (2 if kept else 0) + item_len
            note = _array_note(len(kept) + 1, len(items), cap_tokens)
            if candidate_len + 1 + len(note) > budget:
                break
            kept.append(item)
            head_len = candidate_len
        if kept:
            return kept, True
    elif isinstance(value, dict):
        pairs = list(cast("dict[str, Any]", value).items())
        kept_dict: dict[str, Any] = {}
        head_len = 2  # "{" + "}"
        for key, item in pairs:
            # json's default separators are ", " between pairs and ": " inside.
            pair_len = (
                len(json.dumps(key, ensure_ascii=False, default=str))
                + 2
                + len(json.dumps(item, ensure_ascii=False, default=str))
            )
            candidate_len = head_len + (2 if kept_dict else 0) + pair_len
            note = _dict_note(len(kept_dict) + 1, len(pairs), cap_tokens)
            if candidate_len + 1 + len(note) > budget:
                break
            kept_dict[key] = item
            head_len = candidate_len
        if kept_dict:
            return kept_dict, True
    marker = {"_cam_truncated": True, "note": _text_note(cap_tokens)}
    if len(json.dumps(marker, ensure_ascii=False)) > budget:
        return None, True
    return marker, True
