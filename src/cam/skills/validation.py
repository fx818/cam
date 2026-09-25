from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

import yaml

from cam.access.resolution import ServiceNotFound, parse_service_ref

SKILL_MD = "SKILL.md"
MAX_FILE_BYTES = 100 * 1024
MAX_FILE_COUNT = 50
MAX_TOTAL_BYTES = 4_000_000
TEXT_EXTENSIONS = frozenset({".md", ".txt", ".json", ".yaml", ".csv"})
MAX_NAME_CHARS = 200
MAX_DESCRIPTION_CHARS = 4000
MAX_REQUIRES = 20
_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_ALLOWED_FILES_TEXT = (
    "skills accept text files only (.md, .txt, .json, .yaml, .csv); "
    "scripts and other file types are rejected"
)


class SkillValidationError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SkillSourceFiles:
    files: dict[str, str]
    name: str
    description: str
    requires: tuple[str, ...]

    @property
    def body(self) -> str:
        _meta, _requires, body = split_skill_md(self.files[SKILL_MD])
        return body


def _yaml_string(value: str) -> str:
    if value and not re.search(r"[:#]", value) and value.strip() == value:
        return value
    import json

    return json.dumps(value)


def _strip(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _plain_scalar(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [_plain_scalar(entry) for entry in cast("list[object]", value)]
        return ", ".join(part for part in parts if part)
    if isinstance(value, dict):
        parts = [
            f"{key}: {_plain_scalar(entry)}"
            for key, entry in cast("dict[str, object]", value).items()
        ]
        return ", ".join(part for part in parts if part)
    text = str(value).strip()
    return text


def _requires_items(value: object) -> list[str]:
    items: list[str]
    if value is None:
        return []
    if isinstance(value, list):
        items = [_plain_scalar(entry) for entry in cast("list[object]", value)]
    elif isinstance(value, str):
        text = _strip(value)
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        items = [_strip(part) for part in text.split(",")] if text else []
    else:
        raise SkillValidationError("'requires' must be a list of service refs like [global/pns]")
    return [item for item in items if item]


def _parse_raw_frontmatter(text: str) -> tuple[dict[str, str], list[str], str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise SkillValidationError(f"{SKILL_MD} must start with a YAML frontmatter block")
    end: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        raise SkillValidationError("frontmatter is missing its closing '---' line")
    block = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    try:
        parsed: object = yaml.safe_load(block) or {}
    except yaml.YAMLError as exc:
        raise SkillValidationError(f"frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SkillValidationError("frontmatter must be a YAML mapping of keys to values")
    mapping = cast("dict[str, object]", parsed)
    meta = {str(key): _plain_scalar(value) for key, value in mapping.items()}
    meta.setdefault("name", "")
    meta.setdefault("description", "")
    requires = _requires_items(mapping.get("requires"))
    return meta, requires, body


def split_skill_md(text: str) -> tuple[dict[str, str], list[str], str]:
    meta, requires, body = _parse_raw_frontmatter(text)
    if not meta.get("name") or not meta.get("description"):
        raise SkillValidationError(f"{SKILL_MD} frontmatter must include name and description")
    if len(meta["name"]) > MAX_NAME_CHARS:
        raise SkillValidationError(f"{SKILL_MD} name is too long")
    if len(meta["description"]) > MAX_DESCRIPTION_CHARS:
        raise SkillValidationError(f"{SKILL_MD} description is too long")
    return meta, requires, body


def compose_skill_md(name: str, description: str, requires: list[str], body: str) -> str:
    lines = ["---", f"name: {_yaml_string(name)}", f"description: {_yaml_string(description)}"]
    if requires:
        lines.append("requires:")
        lines.extend(f"  - {ref}" for ref in requires)
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def normalize_path(raw: str) -> str:
    if raw.endswith("/"):
        raise SkillValidationError(f"expected a file path: {raw!r}")
    if "\\" in raw:
        raise SkillValidationError(f"backslashes are not allowed in a skill path: {raw!r}")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise SkillValidationError(f"absolute paths are not allowed in a skill: {raw!r}")
    path = PurePosixPath(raw.replace("\\", "/"))
    parts: list[str] = []
    for part in path.parts:
        if part == ".":
            continue
        if part == "..":
            raise SkillValidationError(f"path traversal is not allowed in a skill: {raw!r}")
        parts.append(part)
    if not parts:
        raise SkillValidationError("empty file path")
    return "/".join(parts)


def _strip_root(files: list[tuple[str, str]]) -> list[tuple[str, str]]:
    if len(files) <= 1:
        return files
    if any(path == SKILL_MD for path, _content in files):
        return files
    roots = {path.split("/", 1)[0] for path, _content in files if "/" in path}
    if len(roots) != 1:
        return files
    root = next(iter(roots))
    adjusted: list[tuple[str, str]] = []
    for path, content in files:
        stripped = path.removeprefix(root + "/")
        if not stripped:
            return files
        adjusted.append((stripped, content))
    return adjusted


def _clean_text(path: str, data: bytes) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix not in TEXT_EXTENSIONS:
        raise SkillValidationError(f"{path!r} is not allowed: {_ALLOWED_FILES_TEXT}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise SkillValidationError(
            f"{path!r} is not UTF-8 text; skills accept text files only"
        ) from None
    if text.startswith("#!"):
        raise SkillValidationError(
            f"{path!r} looks executable (shebang line): {_ALLOWED_FILES_TEXT}"
        )
    return text


def parse_bundle(files: Mapping[str, bytes]) -> SkillSourceFiles:
    entries = [(normalize_path(str(path)), data) for path, data in files.items()]
    if len(entries) > MAX_FILE_COUNT:
        raise SkillValidationError(f"too many files: {len(entries)}, the limit is {MAX_FILE_COUNT}")
    total = 0
    cleaned: list[tuple[str, str]] = []
    for path, data in entries:
        if len(data) > MAX_FILE_BYTES:
            raise SkillValidationError(
                f"{path!r} is too large: {len(data)} bytes, the per-file limit is {MAX_FILE_BYTES}"
            )
        total += len(data)
        cleaned.append((path, _clean_text(path, data)))
    if total > MAX_TOTAL_BYTES:
        raise SkillValidationError(f"the upload is too large in total: {total} bytes")
    cleaned = _strip_root(cleaned)
    file_map = dict(cleaned)
    if SKILL_MD not in file_map:
        raise SkillValidationError(f"{SKILL_MD} is required at the root of a skill")
    meta, requires, _body = split_skill_md(file_map[SKILL_MD])
    deduped = _dedupe_requires(requires)
    if len(deduped) > MAX_REQUIRES:
        raise SkillValidationError(f"'requires' lists more than {MAX_REQUIRES} services")
    return SkillSourceFiles(
        files=file_map,
        name=meta["name"],
        description=meta["description"],
        requires=tuple(deduped),
    )


def _dedupe_requires(requires: list[str] | tuple[str, ...]) -> list[str]:
    seen: list[str] = []
    for raw in requires:
        owner_part, slug = parse_required_ref(raw)
        normalized = f"{owner_part}/{slug}"
        if normalized not in seen:
            seen.append(normalized)
    return seen


def parse_required_ref(raw: str) -> tuple[str, str]:
    try:
        return parse_service_ref(raw)
    except ServiceNotFound:
        raise SkillValidationError(
            f"invalid 'requires' entry {raw!r}: expected a service id like global/pns"
        ) from None


def from_editor(*, name: str, description: str, requires: list[str], body: str) -> SkillSourceFiles:
    resolved_name = name.strip()
    if not resolved_name:
        raise SkillValidationError("name must not be empty")
    resolved_description = description.strip()
    if not resolved_description:
        raise SkillValidationError("description must not be empty")
    return parse_bundle(
        {SKILL_MD: compose_skill_md(resolved_name, resolved_description, requires, body).encode()}
    )


def from_zip(data: bytes) -> SkillSourceFiles:
    entries: dict[str, bytes] = {}
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillValidationError("not a valid zip file") from None
    with archive:
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            mode = entry.external_attr >> 16
            if mode and mode & 0o170000 == 0o120000:
                raise SkillValidationError(
                    f"{entry.filename!r} is a symlink; symlinks are not allowed in skills"
                )
            entries[entry.filename] = archive.read(entry)
    return parse_bundle(entries)


def from_folder(folder: Path) -> SkillSourceFiles:
    root = folder.resolve()
    if not root.is_dir():
        raise SkillValidationError(f"{folder} is not a folder")
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.is_symlink() or path.resolve() != path:
            raise SkillValidationError("symlinks are not allowed in skills")
        files[path.relative_to(root).as_posix()] = path.read_bytes()
    return parse_bundle(files)


def derive_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower()).strip("._-")[:100]
    if not slug or not re.fullmatch(_SLUG_PATTERN, slug):
        raise SkillValidationError(f"cannot derive a slug from the name {name!r}")
    return slug


def valid_slug(slug: str) -> str:
    resolved = slug.strip().lower()
    if not _SLUG_PATTERN.match(resolved):
        raise SkillValidationError(
            "slug must start with a letter or digit and contain only lowercase "
            "letters, digits, dots, underscores or hyphens"
        )
    return resolved
