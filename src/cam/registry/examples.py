"""Sample-argument generation from a stored tool input schema.

One valid-against-schema arg set per tool, generated at import time and shown
as an example invocation in `describe`. Curated examples (set in the tool tab)
always win; this only seeds the first value. Generation is deliberately
conservative: when a required value cannot be built (pattern-bound strings,
deep or unresolvable refs) the whole example is dropped rather than stored in
an invalid shape.
"""

from __future__ import annotations

import re
from typing import Any, Final, cast

_MAX_DEPTH: Final = 6
_MAX_ITEMS: Final = 3

_STRING_FORMATS: Final = {
    "date": "2026-01-01",
    "date-time": "2026-01-01T09:00:00Z",
    "time": "09:00:00",
    "email": "user@example.com",
    "uri": "https://example.com/resource",
    "uuid": "00000000-0000-4000-8000-000000000000",
    "duration": "PT5M",
}

_PATTERN_HINTS: Final = ((re.compile(r"\d{4}-\d{2}-\d{2}"), "2026-01-01"),)


class _Infeasible:
    """Sentinel: an impossible required value invalidates the example."""


_INFEASIBLE = _Infeasible()


class _ImpossibleRequired(Exception):
    """A required value cannot be built: fail the whole example."""


def _sub_dict(value: object) -> dict[str, Any] | None:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _sub_list(value: object) -> list[Any] | None:
    return cast("list[Any]", value) if isinstance(value, list) else None


def _resolve_ref(ref: str, root: dict[str, Any]) -> dict[str, Any] | None:
    if not ref.startswith("#/"):
        return None
    node: object = root
    for part in ref[2:].split("/"):
        mapping = _sub_dict(node)
        if mapping is None or part not in mapping:
            return None
        node = mapping[part]
    return _sub_dict(node)


def _string_sample(schema: dict[str, Any]) -> str | None:
    enum = _sub_list(schema.get("enum"))
    if enum and isinstance(enum[0], str):
        return enum[0]
    default = schema.get("default")
    if isinstance(default, str):
        return default
    fmt = schema.get("format")
    if isinstance(fmt, str) and fmt in _STRING_FORMATS:
        return _STRING_FORMATS[fmt]
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        for regex, hint in _PATTERN_HINTS:
            if regex.search(pattern):
                return hint
        return None
    return "example-value"


def _number_sample(schema: dict[str, Any], *, integer: bool) -> int | float | None:
    enum = _sub_list(schema.get("enum"))
    if enum and isinstance(enum[0], (int, float)) and not isinstance(enum[0], bool):
        return int(enum[0]) if integer else enum[0]
    default = schema.get("default")
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        return default
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)):
        value = int(minimum) if integer else minimum
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            return None
        return value
    return 1 if integer else 1.0


def _object_sample(schema: dict[str, Any], root: dict[str, Any], depth: int) -> dict[str, object]:
    result: dict[str, object] = {}
    required = _sub_list(schema.get("required"))
    required_names = {str(name) for name in required} if required else set[str]()
    properties = _sub_dict(schema.get("properties"))
    raw_properties = cast("dict[object, object]", properties) if properties is not None else {}
    for key, raw_prop in raw_properties.items():
        prop = _sub_dict(raw_prop)
        if prop is None:
            continue
        name = cast("str", key)
        if name in required_names or "default" in prop:
            built = _sample(prop, root, depth + 1)
            if name in required_names and (built is None or isinstance(built, _Infeasible)):
                raise _ImpossibleRequired()
            if built is not None and not isinstance(built, _Infeasible):
                result[name] = built
    if not required_names.issubset(result) or len(result) < int(schema.get("minProperties") or 0):
        raise _ImpossibleRequired()
    return result


def _sample(schema: dict[str, Any], root: dict[str, Any], depth: int) -> object | None:
    """Build one sample value; `_INFEASIBLE` bubbles up on impossible valids."""
    if depth > _MAX_DEPTH:
        return _INFEASIBLE
    try:
        return _try_sample(schema, root, depth)
    except _ImpossibleRequired:
        return _INFEASIBLE


def _try_sample(schema: dict[str, Any], root: dict[str, Any], depth: int) -> object | None:
    enum = _sub_list(schema.get("enum"))
    if enum:
        return enum[0]
    default = schema.get("default")
    if default is not None and not isinstance(default, (dict, list)):
        return default

    for combiner in ("allOf", "oneOf", "anyOf"):
        subspec = _sub_list(schema.get(combiner))
        if subspec:
            first = _sub_dict(subspec[0])
            if first is not None:
                return _sample(first, root, depth + 1)

    ref = schema.get("$ref")
    if isinstance(ref, str):
        target = _resolve_ref(ref, root)
        return _sample(target, root, depth + 1) if target is not None else None

    type_value = schema.get("type")
    type_options = _sub_list(type_value)
    if type_options is not None:
        non_null = [t for t in type_options if t != "null"]
        type_value = non_null[0] if non_null else None

    if type_value == "string":
        return _string_sample(schema)
    if type_value == "integer":
        return _number_sample(schema, integer=True)
    if type_value == "number":
        return _number_sample(schema, integer=False)
    if type_value == "boolean":
        return True
    if type_value == "array":
        items = _sub_dict(schema.get("items"))
        if items is None:
            return []
        built = _sample(items, root, depth + 1)
        if built is None or isinstance(built, _Infeasible):
            return _INFEASIBLE if schema.get("minItems") else []
        min_items = max(1, min(int(schema.get("minItems") or 1), _MAX_ITEMS))
        return [built] * min_items
    if type_value == "object" or "properties" in schema or "required" in schema:
        return _object_sample(schema, root, depth + 1)
    # No usable type hint: fine only when nothing forces a value.
    return None


def sample_invocation(
    input_schema: dict[str, Any] | None,
    *,
    example_override: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The curated example wins; otherwise generate a minimal valid sample."""
    override = _sub_dict(example_override)
    if override:
        return dict(override)
    schema = _sub_dict(input_schema)
    if not schema:
        return None
    built = _sample(schema, schema, depth=0)
    if built is None or isinstance(built, _Infeasible):
        return None
    return _sub_dict(built)
