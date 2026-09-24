import io
import zipfile
from pathlib import Path

import pytest

from cam.skills.validation import (
    SKILL_MD,
    SkillValidationError,
    compose_skill_md,
    derive_slug,
    from_editor,
    from_folder,
    from_zip,
    parse_bundle,
    split_skill_md,
    valid_slug,
)

_FM = "---\nname: PNS analytics\ndescription: How to query PNS\nrequires:\n  - global/pns\n---"
BODY = "# Instructions\nuse global/pns/run_query."


def _skill_md() -> str:
    return f"{_FM}\n{BODY}"


def test_split_skill_md_meta_and_body() -> None:
    meta, requires, body = split_skill_md(_skill_md())
    assert meta["name"] == "PNS analytics"
    assert meta["description"] == "How to query PNS"
    assert requires == ["global/pns"]
    assert body.strip() == BODY


def test_frontmatter_missing_name_rejected() -> None:
    with pytest.raises(SkillValidationError):
        split_skill_md("---\ndescription: d\n---\nbody")


def test_frontmatter_without_closing_rejected() -> None:
    with pytest.raises(SkillValidationError):
        parse_bundle({SKILL_MD: b"---\nname: x\ndescription: y\n"})


def test_parse_bundle_requires_skill_md() -> None:
    with pytest.raises(SkillValidationError):
        parse_bundle({"notes.md": b"hello"})


def test_parse_bundle_inline_requires() -> None:
    text = "---\nname: n\ndescription: d\nrequires: [global/pns, global/redash]\n---\nbody"
    files = parse_bundle({SKILL_MD: text.encode()})
    assert files.requires == ("global/pns", "global/redash")
    assert files.body.strip() == "body"


def test_parse_bundle_rejects_scripts() -> None:
    with pytest.raises(SkillValidationError) as exc:
        parse_bundle({SKILL_MD: _skill_md().encode(), "scripts/run.py": b"x = 1"})
    assert "run.py" in str(exc.value)


@pytest.mark.parametrize("ext", [".sh", ".js", ".ts", ".php", ".rb", ".exe", ".yml", ""])
def test_parse_bundle_rejects_everything_not_text(ext: str) -> None:
    name = "helper" + ext
    with pytest.raises(SkillValidationError):
        parse_bundle({SKILL_MD: _skill_md().encode(), name: b"data"})


def test_parse_bundle_rejects_too_large_file() -> None:
    with pytest.raises(SkillValidationError):
        parse_bundle({SKILL_MD: _skill_md().encode(), "big.md": b"x" * (100 * 1024 + 1)})


def test_parse_bundle_ok_at_limit() -> None:
    files = parse_bundle({SKILL_MD: _skill_md().encode(), "big.md": b"x" * (100 * 1024)})
    assert files.files["big.md"] == "x" * (100 * 1024)


def test_parse_bundle_rejects_too_many_files() -> None:
    files: dict[str, bytes] = {SKILL_MD: _skill_md().encode()}
    for i in range(51):
        files[f"f{i}.md"] = b"note"
    with pytest.raises(SkillValidationError):
        parse_bundle(files)


def test_parse_bundle_traversal_rejected() -> None:
    for bad in (
        "../evil.md",
        "a/../../b.md",
        "./guides/../references.md",
        "/etc/passwd",
        "C:/x.md",
        "a\\b.md",
    ):
        with pytest.raises(SkillValidationError):
            parse_bundle({SKILL_MD: _skill_md().encode(), bad: b"data"})


def _zip_bytes(entries: list[tuple[str, bytes]], mode: int | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries:
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            if mode is not None:
                info.external_attr = mode
            archive.writestr(info, data)
    return buffer.getvalue()


def test_from_zip_strips_single_root() -> None:
    data = _zip_bytes(
        [("pns-anal-p6/SKILL.md", _skill_md().encode()), ("pns-anal-p6/guide.md", b"hi")]
    )
    files = from_zip(data)
    assert list(files.files) == ["SKILL.md", "guide.md"]
    assert files.name == "PNS analytics"
    assert files.requires == ("global/pns",)


def test_from_zip_rejects_binary_content() -> None:
    data = _zip_bytes([("SKILL.md", _skill_md().encode()), ("blob.md", b"\xff\xfe\x00\x11")])
    with pytest.raises(SkillValidationError) as exc:
        from_zip(data)
    assert "UTF-8" in str(exc.value)


def test_from_zip_rejects_symlink() -> None:
    data = _zip_bytes([("SKILL.md", _skill_md().encode())], mode=0o120777 << 16)
    with pytest.raises(SkillValidationError) as exc:
        from_zip(data)
    assert "symlink" in str(exc.value)


def test_from_zip_invalid_error() -> None:
    with pytest.raises(SkillValidationError):
        from_zip(b"not a zip")


def test_from_folder(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    (skill / "references").mkdir(parents=True)
    (skill / SKILL_MD).write_text(_skill_md())
    (skill / "references" / "schema.md").write_text("schema")
    (skill / "references" / "refs.csv").write_text("a,b\n1,2\n")
    files = from_folder(skill)
    assert set(files.files) == {"SKILL.md", "references/schema.md", "references/refs.csv"}
    assert files.description == "How to query PNS"


def test_editor_roundtrip() -> None:
    files = from_editor(name="Team guide", description="d", requires=["global/pns"], body=BODY)
    assert files.name == "Team guide"
    text = compose_skill_md("Team guide", "d", [], "# hi")
    meta, requires, body = split_skill_md(text)
    assert meta["name"] == "Team guide" and requires == [] and body == "# hi"
    with pytest.raises(SkillValidationError):
        from_editor(name="", description="d", requires=[], body="b")


def test_derive_slug_and_valid_slug() -> None:
    assert derive_slug("PNS Analytics!") == "pns-analytics"
    assert valid_slug("My_Skill.v2") == "my_skill.v2"
    with pytest.raises(SkillValidationError):
        derive_slug("!!!")
    with pytest.raises(SkillValidationError):
        valid_slug("Bad Slug")
