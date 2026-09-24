"""Agent-facing CAM errors: envelope shape, codes and upstream mapping.

Every failure surfaced to an agent is a tool result with `isError: true` and
one text block holding `{"error": {...}}`, never a protocol-level failure.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from cam.upstream.errors import UpstreamError


class CamErrorCode(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    INVALID_ARGS = "INVALID_ARGS"
    CONNECTION_EXPIRED = "CONNECTION_EXPIRED"
    OWNER_DEACTIVATED = "OWNER_DEACTIVATED"
    SERVICE_DISABLED = "SERVICE_DISABLED"
    SKILL_UNAVAILABLE = "SKILL_UNAVAILABLE"
    UPSTREAM_AUTH = "UPSTREAM_AUTH"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    UPSTREAM_RATE_LIMITED = "UPSTREAM_RATE_LIMITED"
    CAM_RATE_LIMITED = "CAM_RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    INTERNAL = "INTERNAL"


_RETRYABLE_CODES = frozenset(
    {
        CamErrorCode.UPSTREAM_RATE_LIMITED,
        CamErrorCode.CAM_RATE_LIMITED,
        CamErrorCode.TIMEOUT,
        CamErrorCode.UPSTREAM_UNAVAILABLE,
        CamErrorCode.INTERNAL,
    }
)


class CamError(Exception):
    def __init__(
        self,
        code: CamErrorCode,
        message: str,
        *,
        service: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.service = service
        self.retryable = code in _RETRYABLE_CODES if retryable is None else retryable

    def envelope(self) -> dict[str, Any]:
        return {
            "error": {
                "code": str(self.code),
                "message": self.message,
                "service": self.service,
                "retryable": self.retryable,
            }
        }

    def as_json(self) -> str:
        return json.dumps(self.envelope())


def not_found() -> CamError:
    return CamError(CamErrorCode.NOT_FOUND, "Tool not found or not available.")


def invalid_args(field: str, detail: str) -> CamError:
    return CamError(CamErrorCode.INVALID_ARGS, f"Invalid arguments: {field}: {detail}")


def service_disabled(service: str | None = None) -> CamError:
    return CamError(CamErrorCode.SERVICE_DISABLED, "This service is unavailable.", service=service)


def internal(message: str = "Internal CAM error.") -> CamError:
    return CamError(CamErrorCode.INTERNAL, message)


def skills_unavailable() -> CamError:
    return CamError(CamErrorCode.INTERNAL, "Skills are not yet available in this CAM build.")


def map_upstream_error(exc: UpstreamError, *, service_ref: str, service_name: str) -> CamError:
    code = CamErrorCode(exc.code.value)
    if code is CamErrorCode.TIMEOUT or code is CamErrorCode.UPSTREAM_UNAVAILABLE:
        message = f"{service_name} is unavailable or timed out."
    elif code is CamErrorCode.UPSTREAM_AUTH:
        message = "Credential rejected. Owner needs to reconnect."
    elif code is CamErrorCode.UPSTREAM_RATE_LIMITED:
        message = f"Rate limited by {service_name}, retry later."
    elif code is CamErrorCode.UPSTREAM_ERROR:
        message = exc.message or f"{service_name} returned an error."
    else:
        message = "Internal CAM error."
    return CamError(code, message, service=service_ref)
