from __future__ import annotations

import base64
import json
from typing import cast

from cam.api.errors import ApiProblem

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def parse_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if limit < 1 or limit > MAX_LIMIT:
        raise ApiProblem("VALIDATION_ERROR", f"limit must be between 1 and {MAX_LIMIT}", 422)
    return limit


def encode_cursor(*parts: str) -> str:
    raw = json.dumps(parts).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, *, count: int) -> list[str] | None:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        loaded: object = json.loads(raw)
    except ValueError, json.JSONDecodeError:
        raise ApiProblem("VALIDATION_ERROR", "invalid cursor", 422) from None
    if not isinstance(loaded, list):
        raise ApiProblem("VALIDATION_ERROR", "invalid cursor", 422)
    items = cast("list[object]", loaded)
    if len(items) != count:
        raise ApiProblem("VALIDATION_ERROR", "invalid cursor", 422)
    parts: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ApiProblem("VALIDATION_ERROR", "invalid cursor", 422)
        parts.append(item)
    return parts
