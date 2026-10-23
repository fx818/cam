"""R18 regression: a pooled MCP session must be re-checked against the
current URL policy (and credential material) at the connection boundary, and
retired when that decision changed — never reused under a stale policy."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from cam.upstream import ConnectionTarget, SessionPool, UrlPolicy
from cam.upstream.errors import UpstreamUnavailableError
from fixtures.mock_mcp import MockMCPHandle

ALLOW_LOOPBACK = UrlPolicy(restricted=True, allowlist=("127.0.0.0/8",))
STRICT = UrlPolicy(restricted=True)


@pytest.fixture
async def pool() -> AsyncGenerator[SessionPool]:
    p = SessionPool(idle_timeout_s=60.0, call_timeout_s=10.0)
    yield p
    await p.aclose()


def target(
    handle: MockMCPHandle,
    *,
    policy: UrlPolicy | None = None,
    cid: str = "c1",
    url: str | None = None,
    headers: dict[str, str] | None = None,
) -> ConnectionTarget:
    return ConnectionTarget(
        service_id="test/policy",
        connection_id=cid,
        url=url if url is not None else handle.streamable_url,
        transport="streamable-http",
        headers=headers or {},
        url_policy=policy,
    )


async def test_reuses_session_while_policy_unchanged(
    pool: SessionPool, mock_mcp: MockMCPHandle
) -> None:
    first = await pool.session(target(mock_mcp, policy=ALLOW_LOOPBACK))
    second = await pool.session(target(mock_mcp, policy=ALLOW_LOOPBACK))
    assert first is second
    assert not first.closed


async def test_policy_change_retires_old_session_and_reenforces_policy(
    pool: SessionPool, mock_mcp: MockMCPHandle
) -> None:
    first = await pool.session(target(mock_mcp, policy=ALLOW_LOOPBACK))
    assert not first.closed
    # The freshly loaded policy no longer admits loopback: the pooled session
    # must not be reused; the new policy must be enforced.
    with pytest.raises(UpstreamUnavailableError, match="policy"):
        await pool.session(target(mock_mcp, policy=STRICT))
    assert first.closed


async def test_policy_change_reestablishes_even_when_still_admittable(
    pool: SessionPool, mock_mcp: MockMCPHandle
) -> None:
    stricter_allowlist = UrlPolicy(restricted=True, allowlist=("127.0.0.0/8", "10.0.0.0/8"))
    first = await pool.session(target(mock_mcp, policy=ALLOW_LOOPBACK))
    second = await pool.session(target(mock_mcp, policy=stricter_allowlist))
    assert second is not first
    assert first.closed


async def test_credential_change_invalidates(pool: SessionPool, mock_mcp: MockMCPHandle) -> None:
    first = await pool.session(target(mock_mcp, headers={"Authorization": "Bearer one"}))
    second = await pool.session(target(mock_mcp, headers={"Authorization": "Bearer two"}))
    assert second is not first
    assert first.closed


async def test_url_change_invalidates(pool: SessionPool, mock_mcp: MockMCPHandle) -> None:
    first = await pool.session(target(mock_mcp, url=mock_mcp.streamable_url))
    second = await pool.session(target(mock_mcp, url=f"{mock_mcp.streamable_url}?gen=2"))
    assert second is not first
    assert first.closed


async def test_reuse_does_not_re_resolve(
    pool: SessionPool, mock_mcp: MockMCPHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unchanged policy reuses the session without a second DNS/policy check,
    so the pinned address can never drift between the check and the connect."""
    from cam.upstream import client as client_mod

    real = client_mod.validate_outbound_url
    calls = {"n": 0}

    async def counting(url: str, *, policy: UrlPolicy) -> object:
        calls["n"] += 1
        return await real(url, policy=policy)

    monkeypatch.setattr(client_mod, "validate_outbound_url", counting)
    first = await pool.session(target(mock_mcp, policy=ALLOW_LOOPBACK))
    assert calls["n"] == 1
    second = await pool.session(target(mock_mcp, policy=ALLOW_LOOPBACK))
    assert second is first
    assert calls["n"] == 1


async def test_failed_reestablish_leaves_no_stale_entry(
    pool: SessionPool, mock_mcp: MockMCPHandle
) -> None:
    first = await pool.session(target(mock_mcp, policy=ALLOW_LOOPBACK))
    with pytest.raises(UpstreamUnavailableError):
        await pool.session(target(mock_mcp, policy=STRICT))
    assert first.closed
    # The entry was retired, so a later valid request establishes fresh.
    third = await pool.session(target(mock_mcp, policy=ALLOW_LOOPBACK))
    assert third is not first
    assert not third.closed
