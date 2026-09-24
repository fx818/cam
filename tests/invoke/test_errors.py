"""Unit tests for agent-facing error envelopes and upstream error mapping."""

from __future__ import annotations

import json

from cam.invoke.errors import (
    CamError,
    CamErrorCode,
    internal,
    invalid_args,
    map_upstream_error,
    not_found,
    service_disabled,
    skills_unavailable,
)
from cam.upstream.errors import (
    UpstreamAuthError,
    UpstreamError,
    UpstreamInternalError,
    UpstreamRateLimitedError,
    UpstreamRemoteError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)


def _map(exc: UpstreamError) -> CamError:
    return map_upstream_error(exc, service_ref="global/redash", service_name="Redash")


def test_envelope_shape_and_retryable() -> None:
    error = not_found()
    envelope = error.envelope()
    assert set(envelope["error"]) == {"code", "message", "service", "retryable"}
    assert envelope["error"]["code"] == "NOT_FOUND"
    assert envelope["error"]["message"] == "Tool not found or not available."
    assert envelope["error"]["service"] is None
    assert envelope["error"]["retryable"] is False
    assert json.loads(error.as_json()) == envelope


def test_retryable_codes() -> None:
    assert CamError(CamErrorCode.TIMEOUT, "x").retryable is True
    assert CamError(CamErrorCode.CAM_RATE_LIMITED, "x").retryable is True
    assert CamError(CamErrorCode.UPSTREAM_RATE_LIMITED, "x").retryable is True
    assert CamError(CamErrorCode.UPSTREAM_UNAVAILABLE, "x").retryable is True
    assert CamError(CamErrorCode.INTERNAL, "x").retryable is True
    assert CamError(CamErrorCode.NOT_FOUND, "x").retryable is False
    assert CamError(CamErrorCode.SERVICE_DISABLED, "x").retryable is False


def test_helpers_build_expected_messages() -> None:
    assert invalid_args("args", "must be an object").message == (
        "Invalid arguments: args: must be an object"
    )
    assert service_disabled("global/x").message == "This service is unavailable."
    assert service_disabled("global/x").service == "global/x"
    assert internal().message == "Internal CAM error."
    assert skills_unavailable().message == (
        "This skill is unavailable: a required service is not accessible."
    )


def test_map_timeout_and_unavailable_share_message() -> None:
    for exc in (UpstreamTimeoutError("raw"), UpstreamUnavailableError("raw")):
        mapped = _map(exc)
        assert mapped.code in (CamErrorCode.TIMEOUT, CamErrorCode.UPSTREAM_UNAVAILABLE)
        assert mapped.message == "Redash is unavailable or timed out."
        assert mapped.service == "global/redash"
        assert mapped.retryable is True


def test_map_auth() -> None:
    mapped = _map(UpstreamAuthError("401 unauthorized"))
    assert mapped.code is CamErrorCode.UPSTREAM_AUTH
    assert mapped.message == "Credential rejected. Owner needs to reconnect."
    assert mapped.service == "global/redash"
    assert mapped.retryable is False


def test_map_rate_limited() -> None:
    mapped = _map(UpstreamRateLimitedError("429"))
    assert mapped.code is CamErrorCode.UPSTREAM_RATE_LIMITED
    assert mapped.message == "Rate limited by Redash, retry later."
    assert mapped.retryable is True


def test_map_remote_error_passes_text_through() -> None:
    mapped = _map(UpstreamRemoteError("column does not exist"))
    assert mapped.code is CamErrorCode.UPSTREAM_ERROR
    assert mapped.message == "column does not exist"
    assert mapped.retryable is False


def test_map_internal_fallback() -> None:
    mapped = _map(UpstreamInternalError("boom"))
    assert mapped.code is CamErrorCode.INTERNAL
    assert mapped.message == "Internal CAM error."
    assert mapped.retryable is True
