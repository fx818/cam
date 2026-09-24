"""Unit tests for the 50,000-token response cap."""

from __future__ import annotations

import json
from typing import Any, cast

from cam.invoke.truncation import chars_for_tokens, estimate_tokens, truncate_blocks, truncate_text
from cam.upstream.types import ImageBlock, TextBlock


def test_estimate_tokens_rounds_up() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert chars_for_tokens(50_000) == 200_000


def test_small_text_untouched() -> None:
    text = "short response"
    out, cut = truncate_text(text, max_chars=200, cap_tokens=50)
    assert out == text
    assert cut is False


def test_plain_text_cuts_at_last_newline() -> None:
    text = "line-one\n" + "x" * 10_000
    out, cut = truncate_text(text, max_chars=1_000, cap_tokens=250)
    assert cut is True
    assert out.startswith("line-one\n")
    assert "x" not in out.split("\n", 1)[0].replace("line-one", "")
    assert "[CAM: response truncated" in out
    assert "showing the first ~250 tokens" in out
    assert len(out) <= 1_000


def test_hard_cut_without_newlines_stays_within_budget() -> None:
    text = "y" * 50_000
    out, cut = truncate_text(text, max_chars=1_000, cap_tokens=250)
    assert cut is True
    assert "[CAM: response truncated" in out
    assert len(out) <= 1_000


def test_json_array_keeps_whole_leading_items() -> None:
    data = [{"i": i, "pad": "x" * 20} for i in range(200)]
    text = json.dumps(data)
    out, cut = truncate_text(text, max_chars=4_000, cap_tokens=1_000)
    assert cut is True
    head, note = out.split("\n", 1)
    parsed: Any = json.loads(head)
    assert isinstance(parsed, list)
    kept = cast("list[Any]", parsed)
    assert 0 < len(kept) < 200
    assert kept == data[: len(kept)]
    assert note.startswith("[CAM: response truncated")
    assert "of 200 items" in note
    assert "~1000 token cap" in note
    assert len(out) <= 4_000


def test_json_array_first_item_too_big_falls_back_to_text() -> None:
    text = json.dumps(["z" * 10_000])
    out, cut = truncate_text(text, max_chars=500, cap_tokens=125)
    assert cut is True
    assert "showing the first ~125 tokens" in out
    assert len(out) <= 500


def test_truncate_blocks_cuts_text_and_flags() -> None:
    content = (
        TextBlock(text="a" * 10_000),
        ImageBlock(data="aGVsbG8=", mime_type="image/png"),
    )
    blocks, cut = truncate_blocks(content, 100)
    assert cut is True
    assert len(blocks) == 2
    text_block = blocks[0]
    assert isinstance(text_block, TextBlock)
    assert len(text_block.text) <= 400
    assert blocks[1] == content[1]


def test_truncate_blocks_noop_when_within_cap() -> None:
    content = (TextBlock(text="small"),)
    blocks, cut = truncate_blocks(content, 50_000)
    assert cut is False
    assert blocks == content
