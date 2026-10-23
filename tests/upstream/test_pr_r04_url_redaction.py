"""R04 (upstream part): transport exceptions and log lines must never carry the
credential-bearing query string of a URL."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import httpx2
import pytest

from cam.upstream import pool as pool_mod
from cam.upstream import rest as rest_mod
from cam.upstream.errors import (
    UpstreamUnavailableError,
    map_exception,
    redact,
)
from cam.upstream.pool import SessionPool
from cam.upstream.types import ConnectionTarget

SECRET = "SUPER-SECRET-TOKEN"


@pytest.fixture
async def pool() -> AsyncGenerator[SessionPool]:
    p = SessionPool(idle_timeout_s=60.0, call_timeout_s=10.0)
    yield p
    await p.aclose()


def test_redact_strips_query_and_userinfo() -> None:
    assert redact("for url 'http://h/p?api_key=SECRET'") == "for url 'http://h/p?[redacted]'"
    assert SECRET not in redact(f"http://u:p@h/x?token={SECRET}")
    assert "u:p" not in redact("http://u:p@h/x")
    # A URL with no query or userinfo is untouched.
    assert redact("http://h/p") == "http://h/p"
    assert redact("plain message") == "plain message"


def test_map_exception_redacts_status_error_url() -> None:
    request = httpx2.Request("GET", f"http://up.example.com/x?api_key={SECRET}")
    response = httpx2.Response(500, request=request)
    # Mirrors httpx's raise_for_status message shape, which embeds the URL.
    message = f"Server error '500 Internal Server Error' for url '{request.url}'"
    error = httpx2.HTTPStatusError(message, request=request, response=response)
    mapped, _dead = map_exception(error)
    assert SECRET not in mapped.message
    assert "?[redacted]" in mapped.message


async def test_transport_error_message_never_leaks_the_query_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise httpx.ConnectError(f"failed for url 'http://up.example.com/x?api_key={SECRET}'")

    monkeypatch.setattr(rest_mod.httpx, "AsyncClient", boom)
    with pytest.raises(UpstreamUnavailableError) as excinfo:
        await rest_mod.execute_rest(
            base_url="http://up.example.com",
            binding={"method": "GET", "path": "/x", "params": {}},
            args={},
            query={"api_key": SECRET},
        )
    assert SECRET not in excinfo.value.message
    assert "?[redacted]" in excinfo.value.message


async def test_pool_establish_log_is_redacted(
    pool: SessionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(target: ConnectionTarget) -> object:
        raise UpstreamUnavailableError(f"connect failed for url 'http://up/x?api_key={SECRET}'")

    logged: list[str] = []

    class _FakeLogger:
        def debug(self, msg: str, *args: object, **kwargs: object) -> None:
            logged.append(msg % args if args else msg)

        def warning(self, *args: object, **kwargs: object) -> None:  # pragma: no cover
            pass

        def exception(self, *args: object, **kwargs: object) -> None:  # pragma: no cover
            pass

    monkeypatch.setattr(pool_mod, "logger", _FakeLogger())
    monkeypatch.setattr(pool, "_open_session", failing)
    target = ConnectionTarget(
        service_id="test/redact", connection_id="c1", url="http://up.example.com/mcp"
    )
    with pytest.raises(UpstreamUnavailableError):
        await pool.session(target)
    joined = "\n".join(logged)
    assert logged, "expected the establish-retry log line"
    assert SECRET not in joined
    assert "?[redacted]" in joined
