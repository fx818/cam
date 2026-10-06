"""Argument validation against the stored upstream tool schema.

Runs after access resolution so a caller only ever sees validation for tools
they may reach. A broken stored schema never blocks the call: pass-through
wins over failing closed on metadata we cannot parse.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from jsonschema import exceptions as jsonschema_exceptions
from jsonschema import validate as jsonschema_validate

from cam.invoke.errors import invalid_args

logger = logging.getLogger(__name__)


def _expected_fragment(exc: jsonschema_exceptions.ValidationError) -> str | None:
    raw = getattr(exc, "schema", None)
    if not isinstance(raw, dict):
        return None
    schema = cast("dict[str, Any]", raw)
    raw_enum = schema.get("enum")
    values = cast("list[Any]", raw_enum) if isinstance(raw_enum, list) else None
    if values:
        shown = ", ".join(str(value) for value in values[:5])
        return f"one of: {shown}"
    raw_type = schema.get("type")
    type_options = cast("list[Any]", raw_type) if isinstance(raw_type, list) else None
    if type_options is not None:
        non_null = [entry for entry in type_options if entry != "null"]
        raw_type = non_null[0] if non_null else None
    if isinstance(raw_type, str):
        return f"type {raw_type}"
    return None


def validate_args(schema: dict[str, Any] | None, args: dict[str, Any]) -> None:
    if not schema:
        return
    try:
        jsonschema_validate(args, schema)
    except jsonschema_exceptions.ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path) or "(root)"
        fragment = _expected_fragment(exc)
        detail = exc.message + (f" — expected {fragment}" if fragment else "")
        raise invalid_args(path, detail) from exc
    except jsonschema_exceptions.SchemaError:
        logger.warning("stored tool schema is invalid; skipping argument validation")
        return
    except Exception:
        logger.warning("could not validate tool arguments", exc_info=True)
        return
