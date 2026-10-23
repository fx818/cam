"""Argument validation against the stored upstream tool schema.

Runs after access resolution so a caller only ever sees validation for tools
they may reach. A broken stored schema never blocks the call: pass-through
wins over failing closed on metadata we cannot parse.

Reference safety
----------------
jsonschema's default registry resolves any ``$ref`` it does not already hold
with a *synchronous* ``urllib`` fetch, so a stored tool schema carrying an
``http(s)://`` or ``file://`` reference could make CAM read the network or the
filesystem from inside the request path (SSRF + event-loop block). We always
pass an explicit registry whose ``retrieve`` refuses every external fetch, so
only references already present in the schema in hand (``#/...`` fragments,
``$defs`` and the standard ``$schema`` meta schemas) resolve. A schema that
needs any other reference is rejected by the registry and treated like any
unparseable schema: log and skip.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import referencing
import referencing.jsonschema
from jsonschema import exceptions as jsonschema_exceptions
from jsonschema import validate as jsonschema_validate
from referencing.exceptions import NoSuchResource

from cam.invoke.errors import invalid_args

logger = logging.getLogger(__name__)


def _reject_external_retrieval(uri: str) -> referencing.jsonschema.SchemaResource:
    """Refuse to resolve any reference the local schema does not already provide.

    Raising here stops jsonschema from ever calling ``urllib.request.urlopen``
    (remote) or reading a ``file://`` path for an attacker-supplied ``$ref``.
    """
    raise NoSuchResource(ref=uri)


# A registry that owns no resources of its own. Any ``$ref`` the schema in hand
# does not already provide raises ``NoSuchResource`` instead of being fetched.
LOCAL_ONLY_REGISTRY: referencing.jsonschema.SchemaRegistry = referencing.Registry(
    retrieve=_reject_external_retrieval
)


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
        jsonschema_validate(args, schema, registry=LOCAL_ONLY_REGISTRY)
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
