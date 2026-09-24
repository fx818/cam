"""Argument validation against the stored upstream tool schema.

Runs after access resolution so a caller only ever sees validation for tools
they may reach. A broken stored schema never blocks the call: pass-through
wins over failing closed on metadata we cannot parse.
"""

from __future__ import annotations

import logging
from typing import Any

from jsonschema import exceptions as jsonschema_exceptions
from jsonschema import validate as jsonschema_validate

from cam.invoke.errors import invalid_args

logger = logging.getLogger(__name__)


def validate_args(schema: dict[str, Any] | None, args: dict[str, Any]) -> None:
    if not schema:
        return
    try:
        jsonschema_validate(args, schema)
    except jsonschema_exceptions.ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path) or "(root)"
        raise invalid_args(path, exc.message) from exc
    except jsonschema_exceptions.SchemaError:
        logger.warning("stored tool schema is invalid; skipping argument validation")
        return
    except Exception:
        logger.warning("could not validate tool arguments", exc_info=True)
        return
