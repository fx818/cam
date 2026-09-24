"""Upstream MCP client surface: sessions, pooling, normalization, errors."""

from cam.upstream.client import UpstreamSession
from cam.upstream.errors import (
    ErrorCode,
    UpstreamAuthError,
    UpstreamError,
    UpstreamInternalError,
    UpstreamRateLimitedError,
    UpstreamRemoteError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
    map_exception,
)
from cam.upstream.normalize import normalize_call_result
from cam.upstream.pool import SessionPool, pool_key
from cam.upstream.types import (
    ConnectionTarget,
    ContentBlock,
    ImageBlock,
    TextBlock,
    ToolCallOutcome,
    UpstreamTool,
)
from cam.upstream.url import PinnedDestination, UrlPolicy, UrlPolicyError, admit_redirect, admit_url

__all__ = [
    "ConnectionTarget",
    "ContentBlock",
    "ErrorCode",
    "ImageBlock",
    "PinnedDestination",
    "SessionPool",
    "TextBlock",
    "ToolCallOutcome",
    "UpstreamAuthError",
    "UpstreamError",
    "UpstreamInternalError",
    "UpstreamRateLimitedError",
    "UpstreamRemoteError",
    "UpstreamSession",
    "UpstreamTimeoutError",
    "UpstreamTool",
    "UpstreamUnavailableError",
    "UrlPolicy",
    "UrlPolicyError",
    "admit_redirect",
    "admit_url",
    "map_exception",
    "normalize_call_result",
    "pool_key",
]
