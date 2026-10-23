"""Typed error surface for upstream MCP operations.

These mirror the Implementation spec's error-table codes that can originate
from an upstream MCP call; the gateway maps them to agent-facing results.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum
from typing import ClassVar, Final, cast

import anyio
import httpx2
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

_QUERY_RE: Final = re.compile(r"\?[^\s'\"]*")
_USERINFO_RE: Final = re.compile(r"//[^\s/'\"@]+:[^\s/'\"@]+@")


class ErrorCode(StrEnum):
    TIMEOUT = "TIMEOUT"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    UPSTREAM_AUTH = "UPSTREAM_AUTH"
    UPSTREAM_RATE_LIMITED = "UPSTREAM_RATE_LIMITED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    INTERNAL = "INTERNAL"


class UpstreamError(Exception):
    """Base for every failure surfaced out of `cam.upstream`."""

    code: ClassVar[ErrorCode]
    retryable: ClassVar[bool]

    def __init__(self, message: str, *, retries_exhausted: bool = False) -> None:
        super().__init__(message)
        self.message = message
        # True when the establish path already consumed its single reconnect
        # (e.g. the legacy→auto mode fallback ran and also failed), so an
        # outer layer must not retry the session again (R35: "one reconnect").
        self.retries_exhausted = retries_exhausted


class UpstreamTimeoutError(UpstreamError):
    code = ErrorCode.TIMEOUT
    retryable = True


class UpstreamUnavailableError(UpstreamError):
    code = ErrorCode.UPSTREAM_UNAVAILABLE
    retryable = True


class UpstreamAuthError(UpstreamError):
    code = ErrorCode.UPSTREAM_AUTH
    retryable = False


class UpstreamRateLimitedError(UpstreamError):
    code = ErrorCode.UPSTREAM_RATE_LIMITED
    retryable = True


class UpstreamRemoteError(UpstreamError):
    """The upstream answered with a protocol/tool-level error."""

    code = ErrorCode.UPSTREAM_ERROR
    retryable = False


class UpstreamInternalError(UpstreamError):
    code = ErrorCode.INTERNAL
    retryable = True


def redact(text: str) -> str:
    """Strip credential-bearing URL parts (query string, userinfo) from `text`.

    Transport/SDK exceptions embed the request URL, which for personal
    services can carry `?api_key=…`/`?token=…` credentials; a raw `str(exc)`
    in an exception message or log line would leak them. This is the single
    redaction helper every message/log line in `cam.upstream` must pass
    through (R04).
    """
    text = _USERINFO_RE.sub("//[redacted]@", text)
    return _QUERY_RE.sub("?[redacted]", text)


def _flatten(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        group = cast("BaseExceptionGroup[BaseException]", exc)
        leaves: list[BaseException] = []
        for item in group.exceptions:
            leaves.extend(_flatten(item))
        return leaves
    return [exc]


def map_exception(
    exc: BaseException, *, recent_statuses: Sequence[int] = ()
) -> tuple[UpstreamError, bool]:
    """Map a transport/SDK failure to an `UpstreamError`.

    Returns `(error, session_dead)`: `session_dead` is True when the failure
    makes the pooled session unusable, so the pool reconnects on next acquire.
    `recent_statuses` are HTTP status codes observed during the failed
    operation (the streamable-HTTP transport folds non-2xx responses into
    generic JSON-RPC errors, so the status log is what recovers 401/403/429).
    """
    if isinstance(exc, UpstreamError):
        return exc, False
    leaves = _flatten(exc)
    message = redact(str(leaves[0])) if leaves else type(exc).__name__

    if any(status in (401, 403) for status in recent_statuses):
        return UpstreamAuthError(message), False
    if 429 in recent_statuses:
        return UpstreamRateLimitedError(message), False
    if any(status >= 500 for status in recent_statuses):
        return UpstreamUnavailableError(message), False

    for leaf in leaves:
        if isinstance(leaf, MCPError):
            if leaf.code == REQUEST_TIMEOUT:
                return UpstreamTimeoutError(redact(leaf.message)), False
            if leaf.code == CONNECTION_CLOSED:
                return UpstreamUnavailableError(redact(leaf.message)), True
            return UpstreamRemoteError(redact(leaf.message)), False
        if isinstance(leaf, httpx2.HTTPStatusError):
            status = leaf.response.status_code
            text = redact(str(leaf))
            if status in (401, 403):
                return UpstreamAuthError(text), False
            if status == 429:
                return UpstreamRateLimitedError(text), False
            if status >= 500:
                return UpstreamUnavailableError(text), False
            return UpstreamRemoteError(text), False
        if isinstance(leaf, httpx2.TimeoutException):
            return UpstreamTimeoutError(redact(str(leaf))), False
        if isinstance(leaf, httpx2.HTTPError):
            return UpstreamUnavailableError(redact(str(leaf))), True
        if isinstance(leaf, TimeoutError):
            return UpstreamTimeoutError(redact(str(leaf))), False
        if isinstance(leaf, (anyio.BrokenResourceError, anyio.ClosedResourceError)):
            return UpstreamUnavailableError(redact(str(leaf))), True
        if isinstance(leaf, OSError):
            return UpstreamUnavailableError(redact(str(leaf))), True
        if isinstance(leaf, Exception):
            return UpstreamInternalError(f"{type(leaf).__name__}: {message}"), False
    return UpstreamInternalError(message), False
