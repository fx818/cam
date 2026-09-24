"""REST executor tests: request building, status mapping, policy, redirects."""

from __future__ import annotations

import json

import pytest

from cam.upstream.errors import (
    UpstreamAuthError,
    UpstreamRateLimitedError,
    UpstreamRemoteError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from cam.upstream.rest import RestBindingError, build_rest_request, execute_rest, fetch_text
from cam.upstream.types import TextBlock, ToolCallOutcome
from cam.upstream.url import UrlPolicy
from fixtures.mock_rest import API_KEY_NAME, API_KEY_VALUE, MockRestHandle

AUTH = {API_KEY_NAME: API_KEY_VALUE}


def _text(outcome: ToolCallOutcome) -> str:
    block = outcome.content[0]
    assert isinstance(block, TextBlock)
    return block.text


def test_build_places_path_query_header_and_body() -> None:
    request = build_rest_request(
        "http://api.example.com",
        {
            "method": "POST",
            "path": "/v1/things/{thing_id}",
            "params": {
                "thing_id": "path",
                "q": "query",
                "X-Trace": "header",
                "name": "body",
            },
        },
        {"thing_id": "a b", "q": "filter", "X-Trace": "t-1", "name": "named", "extra": 1},
        headers={"Authorization": "Bearer tok"},
        query={"api_key": "k"},
    )
    assert request.method == "POST"
    assert request.url == "http://api.example.com/v1/things/a%20b"
    assert request.params == {"q": "filter", "api_key": "k"}
    assert request.headers == {"X-Trace": "t-1", "Authorization": "Bearer tok"}
    assert request.json_body == {"name": "named", "extra": 1}


def test_build_defaults_post_body_and_get_query() -> None:
    post = build_rest_request(
        "http://api.example.com",
        {"method": "POST", "path": "/items", "params": {}},
        {"name": "x"},
    )
    assert post.json_body == {"name": "x"}
    get = build_rest_request(
        "http://api.example.com",
        {"method": "GET", "path": "/items", "params": {}},
        {"q": "x"},
    )
    assert get.json_body is None
    assert get.params == {"q": "x"}


def test_build_credential_headers_override_argument_headers() -> None:
    request = build_rest_request(
        "http://api.example.com",
        {"method": "GET", "path": "/x", "params": {"Authorization": "header"}},
        {"Authorization": "spoofed"},
        headers={"Authorization": "Bearer real"},
    )
    assert request.headers["Authorization"] == "Bearer real"


def test_build_rejects_bad_bindings() -> None:
    with pytest.raises(RestBindingError, match="method"):
        build_rest_request("http://api.example.com", {"method": "FLY", "path": "/x"}, {})
    with pytest.raises(RestBindingError, match="path"):
        build_rest_request("http://api.example.com", {"method": "GET", "path": ""}, {})
    with pytest.raises(RestBindingError, match="relative"):
        build_rest_request("http://api.example.com", {"method": "GET", "path": "http://evil/x"}, {})
    with pytest.raises(RestBindingError, match="location"):
        build_rest_request(
            "http://api.example.com",
            {"method": "GET", "path": "/x", "params": {"a": "moon"}},
            {},
        )
    with pytest.raises(RestBindingError, match="missing path parameter"):
        build_rest_request("http://api.example.com", {"method": "GET", "path": "/x/{id}"}, {})


async def test_execute_get_json_with_api_key(mock_rest: MockRestHandle) -> None:
    outcome = await execute_rest(
        base_url=mock_rest.base_url,
        binding={"method": "GET", "path": "/items", "params": {"q": "query"}},
        args={"q": "beta"},
        headers=AUTH,
    )
    assert outcome.is_error is False
    rows = json.loads(_text(outcome))
    assert [row["name"] for row in rows] == ["beta"]
    last = mock_rest.requests[-1]
    assert last.headers[API_KEY_NAME.lower()] == API_KEY_VALUE
    assert last.query == {"q": "beta"}


async def test_execute_path_param_and_post_body(mock_rest: MockRestHandle) -> None:
    detail = await execute_rest(
        base_url=mock_rest.base_url,
        binding={
            "method": "GET",
            "path": "/items/{item_id}",
            "params": {"item_id": "path"},
        },
        args={"item_id": "i-1"},
        headers=AUTH,
    )
    assert json.loads(_text(detail))["id"] == "i-1"

    created = await execute_rest(
        base_url=mock_rest.base_url,
        binding={
            "method": "POST",
            "path": "/items",
            "params": {"name": "body", "tags": "body"},
        },
        args={"name": "delta", "tags": ["new"]},
        headers=AUTH,
    )
    created_text = _text(created)
    assert created_text
    payload = json.loads(created_text)
    assert payload["name"] == "delta"
    assert mock_rest.requests[-1].body == {"name": "delta", "tags": ["new"]}


async def test_execute_plain_text_response(mock_rest: MockRestHandle) -> None:
    outcome = await execute_rest(
        base_url=mock_rest.base_url,
        binding={"method": "GET", "path": "/plain", "params": {}},
        args={},
        headers=AUTH,
    )
    assert _text(outcome) == "hello from mock rest"
    assert outcome.structured is None


async def test_execute_401_maps_to_auth_error(mock_rest: MockRestHandle) -> None:
    with pytest.raises(UpstreamAuthError):
        await execute_rest(
            base_url=mock_rest.base_url,
            binding={"method": "GET", "path": "/items", "params": {}},
            args={},
        )


async def test_execute_wrong_api_key_maps_to_auth_error(mock_rest: MockRestHandle) -> None:
    with pytest.raises(UpstreamAuthError):
        await execute_rest(
            base_url=mock_rest.base_url,
            binding={"method": "GET", "path": "/items", "params": {}},
            args={},
            headers={API_KEY_NAME: "wrong"},
        )


async def test_execute_404_passes_upstream_text(mock_rest: MockRestHandle) -> None:
    with pytest.raises(UpstreamRemoteError) as excinfo:
        await execute_rest(
            base_url=mock_rest.base_url,
            binding={"method": "GET", "path": "/missing", "params": {}},
            args={},
        )
    assert "no such thing" in excinfo.value.message


async def test_execute_500_maps_to_unavailable(mock_rest: MockRestHandle) -> None:
    with pytest.raises(UpstreamUnavailableError):
        await execute_rest(
            base_url=mock_rest.base_url,
            binding={"method": "GET", "path": "/boom", "params": {}},
            args={},
        )


async def test_execute_429_maps_to_rate_limited(mock_rest: MockRestHandle) -> None:
    with pytest.raises(UpstreamRateLimitedError):
        await execute_rest(
            base_url=mock_rest.base_url,
            binding={"method": "GET", "path": "/throttled", "params": {}},
            args={},
        )


async def test_execute_timeout_maps_to_timeout(mock_rest: MockRestHandle) -> None:
    with pytest.raises(UpstreamTimeoutError):
        await execute_rest(
            base_url=mock_rest.base_url,
            binding={
                "method": "GET",
                "path": "/slow",
                "params": {"seconds": "query"},
            },
            args={"seconds": 5.0},
            timeout_ms=300,
        )


async def test_execute_follows_validated_redirect_with_credential(
    mock_rest: MockRestHandle,
) -> None:
    outcome = await execute_rest(
        base_url=mock_rest.base_url,
        binding={"method": "GET", "path": "/redirect", "params": {}},
        args={},
        headers=AUTH,
    )
    rows = json.loads(_text(outcome))
    assert isinstance(rows, list)
    followed = [request for request in mock_rest.requests[-2:] if request.path == "/items"]
    assert followed and followed[-1].headers[API_KEY_NAME.lower()] == API_KEY_VALUE


async def test_restricted_policy_blocks_loopback_without_allowlist(
    mock_rest: MockRestHandle,
) -> None:
    with pytest.raises(UpstreamUnavailableError, match="blocked"):
        await execute_rest(
            base_url=mock_rest.base_url,
            binding={"method": "GET", "path": "/open", "params": {}},
            args={},
            url_policy=UrlPolicy(restricted=True),
        )


async def test_restricted_policy_allows_allowlisted_cidr(mock_rest: MockRestHandle) -> None:
    outcome = await execute_rest(
        base_url=mock_rest.base_url,
        binding={"method": "GET", "path": "/open", "params": {}},
        args={},
        url_policy=UrlPolicy(restricted=True, allowlist=("127.0.0.0/8",)),
    )
    assert json.loads(_text(outcome)) == {"status": "pong"}


async def test_fetch_text_reads_openapi_spec(mock_rest: MockRestHandle) -> None:
    text = await fetch_text(mock_rest.spec_url)
    spec = json.loads(text)
    assert spec["info"]["title"] == "CAM Mock REST API"
