"""R05 regression: redirects must not forward credentials or replay the
operation to a different origin, and must apply safe method/body semantics."""

from __future__ import annotations

import json

import pytest
from _pr_servers import start_recorder

from cam.upstream import rest as rest_mod
from cam.upstream.errors import UpstreamUnavailableError
from cam.upstream.rest import build_rest_request, execute_rest

_redirect_method = rest_mod._redirect_method  # pyright: ignore[reportPrivateUsage]

SECRET_HEADER = {"Authorization": "Bearer SUPER-SECRET"}
SECRET_QUERY = {"api_key": "SUPER-SECRET"}


def test_redirect_method_semantics() -> None:
    # 303 always becomes GET with no body; HEAD stays HEAD.
    assert _redirect_method("POST", 303) == ("GET", False)
    assert _redirect_method("HEAD", 303) == ("HEAD", False)
    # 301/302 downgrade mutating methods to GET and never keep the body.
    assert _redirect_method("POST", 301) == ("GET", False)
    assert _redirect_method("PUT", 302) == ("GET", False)
    assert _redirect_method("GET", 302) == ("GET", False)
    # 307/308 preserve method and body for same-origin hops.
    assert _redirect_method("PUT", 307) == ("PUT", True)
    assert _redirect_method("DELETE", 308) == ("DELETE", True)


def test_build_marks_only_injected_credentials() -> None:
    request = build_rest_request(
        "http://api.example.com",
        {"method": "GET", "path": "/x", "params": {"X-Trace": "header"}},
        {"X-Trace": "t-1"},
        headers={"Authorization": "Bearer tok"},
        query={"api_key": "k"},
    )
    assert request.credential_header_names == frozenset({"Authorization"})
    assert request.credential_param_names == frozenset({"api_key"})
    # A caller-supplied argument header is not a credential.
    assert "X-Trace" not in request.credential_header_names


async def test_cross_origin_redirect_drops_credential_header_and_query() -> None:
    target = await start_recorder()
    source = await start_recorder(redirect_to=f"{target.base_url}/target", redirect_status=302)
    try:
        outcome = await execute_rest(
            base_url=source.base_url,
            binding={"method": "GET", "path": "/start", "params": {}},
            args={},
            headers=SECRET_HEADER,
            query=SECRET_QUERY,
        )
        assert outcome.is_error is False
        # The original origin received the credential.
        assert source.calls[-1].headers.get("authorization") == "Bearer SUPER-SECRET"
        assert source.calls[-1].query == {"api_key": "SUPER-SECRET"}
        # The redirected-to origin must not.
        redirected = [c for c in target.calls if c.path == "/target"][-1]
        assert redirected.method == "GET"
        assert "authorization" not in redirected.headers
        assert "api_key" not in redirected.query
    finally:
        await source.aclose()
        await target.aclose()


async def test_cross_origin_post_is_downgraded_and_body_dropped() -> None:
    target = await start_recorder()
    source = await start_recorder(redirect_to=f"{target.base_url}/target", redirect_status=302)
    try:
        await execute_rest(
            base_url=source.base_url,
            binding={"method": "POST", "path": "/start", "params": {"name": "body"}},
            args={"name": "created"},
            headers=SECRET_HEADER,
        )
        # The mutating method and its body are never replayed cross-origin.
        assert source.calls[-1].method == "POST"
        redirected = [c for c in target.calls if c.path == "/target"][-1]
        assert redirected.method == "GET"
        assert redirected.body in (None, b"")
    finally:
        await source.aclose()
        await target.aclose()


async def test_303_becomes_get_without_body() -> None:
    target = await start_recorder()
    source = await start_recorder(redirect_to=f"{target.base_url}/target", redirect_status=303)
    try:
        await execute_rest(
            base_url=source.base_url,
            binding={"method": "POST", "path": "/start", "params": {"name": "body"}},
            args={"name": "created"},
            headers=SECRET_HEADER,
        )
        redirected = [c for c in target.calls if c.path == "/target"][-1]
        assert redirected.method == "GET"
        assert redirected.body in (None, b"")
    finally:
        await source.aclose()
        await target.aclose()


async def test_same_origin_307_preserves_method_body_and_credential() -> None:
    source = await start_recorder(redirect_to="/target", redirect_status=307)
    try:
        await execute_rest(
            base_url=source.base_url,
            binding={"method": "POST", "path": "/start", "params": {"name": "body"}},
            args={"name": "kept"},
            headers=SECRET_HEADER,
        )
        recorded = {call.path: call for call in source.calls}
        target = recorded["/target"]
        assert target.method == "POST"
        assert target.body is not None
        assert json.loads(target.body) == {"name": "kept"}
        # Same-origin hops keep the credential (policy pinning still applies).
        assert target.headers.get("authorization") == "Bearer SUPER-SECRET"
    finally:
        await source.aclose()


async def test_cross_origin_redirect_argument_header_is_forwarded() -> None:
    """Only the injected credential is stripped, not ordinary argument headers."""
    target = await start_recorder()
    source = await start_recorder(redirect_to=f"{target.base_url}/target", redirect_status=302)
    try:
        await execute_rest(
            base_url=source.base_url,
            binding={"method": "GET", "path": "/start", "params": {"X-Trace": "header"}},
            args={"X-Trace": "t-123"},
            headers=SECRET_HEADER,
        )
        redirected = [c for c in target.calls if c.path == "/target"][-1]
        assert redirected.headers.get("x-trace") == "t-123"
        assert "authorization" not in redirected.headers
    finally:
        await source.aclose()
        await target.aclose()


async def test_cross_origin_redirect_hop_is_still_policy_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cross-origin redirect to a blocked destination is refused by policy."""
    target = await start_recorder()
    source = await start_recorder(redirect_to=f"{target.base_url}/target", redirect_status=302)
    try:
        from cam.upstream import rest as rest_mod

        async def reject(*args: object, **kwargs: object) -> object:
            raise ValueError("blocked by test policy")

        monkeypatch.setattr(rest_mod, "admit_redirect", reject)
        with pytest.raises(UpstreamUnavailableError, match="redirect"):
            await execute_rest(
                base_url=source.base_url,
                binding={"method": "GET", "path": "/start", "params": {}},
                args={},
                headers=SECRET_HEADER,
            )
    finally:
        await source.aclose()
        await target.aclose()
