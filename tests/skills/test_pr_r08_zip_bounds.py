"""R08: skill zip limits are enforced before decompression, and YAML is bounded."""

from __future__ import annotations

import io
import zipfile

import pytest

from cam.skills.validation import (
    MAX_FRONTMATTER_BYTES,
    SKILL_MD,
    SkillValidationError,
    from_zip,
    from_zip_async,
    parse_bundle,
)

_FM = "---\nname: PNS analytics\ndescription: How to query PNS\n---"
BODY = "# hi"


def _skill_md() -> str:
    return f"{_FM}\n{BODY}"


def _zip_bytes(entries: list[tuple[str, bytes]], *, compress: bool = False) -> bytes:
    buffer = io.BytesIO()
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buffer, "w", mode) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def test_zip_rejects_too_many_entries_before_reading() -> None:
    entries = [(SKILL_MD, _skill_md().encode())]
    entries += [(f"f{i}.md", b"x") for i in range(50)]
    with pytest.raises(SkillValidationError, match="too many files"):
        from_zip(_zip_bytes(entries))


def test_zip_rejects_declared_total_over_limit() -> None:
    entries = [(SKILL_MD, _skill_md().encode())]
    entries += [(f"f{i}.md", b"a" * (100 * 1024)) for i in range(41)]
    with pytest.raises(SkillValidationError, match="too large in total"):
        from_zip(_zip_bytes(entries))


def test_zip_rejects_oversized_declared_entry() -> None:
    entries = [(SKILL_MD, _skill_md().encode()), ("big.md", b"a" * (100 * 1024 + 1))]
    with pytest.raises(SkillValidationError, match="too large"):
        from_zip(_zip_bytes(entries))


def test_zip_rejects_high_compression_ratio() -> None:
    payload = b"\x00" * (90 * 1024)
    entries = [(SKILL_MD, _skill_md().encode()), ("notes.md", payload)]
    with pytest.raises(SkillValidationError, match="too compressed"):
        from_zip(_zip_bytes(entries, compress=True))


def test_zip_preserves_symlink_rejection() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo(SKILL_MD, (2026, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        archive.writestr(info, _skill_md())
    with pytest.raises(SkillValidationError, match="symlink"):
        from_zip(buffer.getvalue())


def test_zip_preserves_binary_rejection() -> None:
    entries = [(SKILL_MD, _skill_md().encode()), ("blob.md", b"\xff\xfe\x00\x11")]
    with pytest.raises(SkillValidationError, match="UTF-8"):
        from_zip(_zip_bytes(entries))


async def test_from_zip_async_parses_bundle() -> None:
    files = await from_zip_async(_zip_bytes([(SKILL_MD, _skill_md().encode())]))
    assert files.name == "PNS analytics"


def test_yaml_anchors_and_aliases_rejected() -> None:
    text = "---\nname: x\ndescription: d\nextra: &anchor foo\nother: *anchor\n---\nbody"
    with pytest.raises(SkillValidationError, match="anchor"):
        parse_bundle({SKILL_MD: text.encode()})


def test_yaml_complexity_bounded() -> None:
    items = "\n".join(["- x"] * 6000)
    text = f"---\nname: x\ndescription: d\nextra:\n{items}\n---\nbody"
    with pytest.raises(SkillValidationError, match="complex"):
        parse_bundle({SKILL_MD: text.encode()})


def test_frontmatter_size_bounded() -> None:
    big = "d" * (MAX_FRONTMATTER_BYTES + 100)
    text = f"---\nname: x\ndescription: {big}\n---\nbody"
    with pytest.raises(SkillValidationError, match="too large"):
        parse_bundle({SKILL_MD: text.encode()})
