"""URL admission choke-point tests."""

import socket

import pytest

from cam.upstream.url import (
    MAX_REDIRECT_HOPS,
    PinnedDestination,
    Resolver,
    UrlPolicy,
    UrlPolicyError,
    admit_redirect,
    admit_url,
    validate_outbound_url,
)


def _literal_url(host: str) -> str:
    return f"https://[{host}]/mcp" if ":" in host else f"https://{host}/mcp"


def _resolver(addresses: list[str]) -> Resolver:
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolve


async def _never_resolve(host: str, port: int) -> list[str]:
    raise AssertionError(f"resolver must not run (got {host}:{port})")


def test_http_and_https_accepted() -> None:
    assert admit_url("http://example.com/mcp") == "http://example.com/mcp"
    assert admit_url("https://example.com/mcp") == "https://example.com/mcp"


@pytest.mark.parametrize("url", ["ftp://example.com", "file:///etc/passwd", "ws://example.com"])
def test_rejects_non_http_schemes(url: str) -> None:
    with pytest.raises(ValueError, match="scheme"):
        admit_url(url)


def test_rejects_missing_host() -> None:
    with pytest.raises(ValueError, match="host"):
        admit_url("http:///path")


def test_rejects_embedded_credentials() -> None:
    with pytest.raises(ValueError, match="credentials"):
        admit_url("http://user:pass@example.com/mcp")


def test_unrestricted_admits_loopback_and_internal_hosts() -> None:
    assert admit_url("http://127.0.0.1:9/mcp") == "http://127.0.0.1:9/mcp"
    assert admit_url("https://localhost/mcp") == "https://localhost/mcp"


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.1.2.3",
        "100.64.0.5",
        "169.254.169.254",
        "172.16.0.1",
        "192.168.1.1",
        "0.0.0.0",
        "::1",
        "fd00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
    ],
)
def test_restricted_blocks_internal_literals(host: str) -> None:
    with pytest.raises(UrlPolicyError, match="blocked"):
        admit_url(_literal_url(host), restricted=True)


@pytest.mark.parametrize(
    "host",
    ["localhost", "app.localhost", "metadata.google.internal", "LOCALHOST"],
)
def test_restricted_blocks_reserved_hostnames(host: str) -> None:
    with pytest.raises(UrlPolicyError, match="blocked"):
        admit_url(f"https://{host}/mcp", restricted=True)


def test_restricted_admits_public_hostname_without_dns() -> None:
    assert admit_url("https://owned.example.com/mcp", restricted=True) == (
        "https://owned.example.com/mcp"
    )


def test_allowlist_exempts_hostname() -> None:
    url = "http://localhost:8080/mcp"
    assert admit_url(url, restricted=True, allowlist=("localhost",)) == url


def test_allowlist_exempts_literal_cidr() -> None:
    url = "http://10.1.2.3/mcp"
    assert admit_url(url, restricted=True, allowlist=("10.0.0.0/8",)) == url


def test_allowlist_does_not_exempt_other_ranges() -> None:
    with pytest.raises(UrlPolicyError, match="blocked"):
        admit_url("http://10.1.2.3/mcp", restricted=True, allowlist=("192.168.0.0/16",))


async def test_validate_pins_first_public_ip() -> None:
    pinned = await validate_outbound_url(
        "https://owned.example.com/mcp",
        policy=UrlPolicy(restricted=True),
        resolver=_resolver(["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]),
    )
    assert pinned == PinnedDestination(host="owned.example.com", port=443, ip="93.184.216.34")


async def test_validate_rejects_blocked_resolution() -> None:
    with pytest.raises(UrlPolicyError, match="blocked address"):
        await validate_outbound_url(
            "https://evil.example.com/mcp",
            policy=UrlPolicy(restricted=True),
            resolver=_resolver(["10.0.0.5"]),
        )


async def test_validate_rejects_when_any_address_blocked() -> None:
    with pytest.raises(UrlPolicyError, match="blocked address"):
        await validate_outbound_url(
            "https://evil.example.com/mcp",
            policy=UrlPolicy(restricted=True),
            resolver=_resolver(["93.184.216.34", "127.0.0.1"]),
        )


async def test_hostname_allowlist_skips_resolution() -> None:
    pinned = await validate_outbound_url(
        "https://internal.example.com/mcp",
        policy=UrlPolicy(restricted=True, allowlist=("internal.example.com",)),
        resolver=_never_resolve,
    )
    assert pinned.ip is None


async def test_dns_failure_maps_to_policy_error() -> None:
    async def fail(host: str, port: int) -> list[str]:
        raise socket.gaierror(f"NXDOMAIN {host}:{port}")

    with pytest.raises(UrlPolicyError, match="cannot resolve"):
        await validate_outbound_url(
            "https://ghost.example.com/mcp",
            policy=UrlPolicy(restricted=True),
            resolver=fail,
        )


async def test_empty_resolution_rejected() -> None:
    with pytest.raises(UrlPolicyError, match="no addresses"):
        await validate_outbound_url(
            "https://empty.example.com/mcp",
            policy=UrlPolicy(restricted=True),
            resolver=_resolver([]),
        )


async def test_invalid_address_rejected() -> None:
    with pytest.raises(UrlPolicyError, match="invalid address"):
        await validate_outbound_url(
            "https://bad.example.com/mcp",
            policy=UrlPolicy(restricted=True),
            resolver=_resolver(["not-an-ip"]),
        )


async def test_literal_pinned_without_resolver_when_allowlisted() -> None:
    pinned = await validate_outbound_url(
        "http://127.0.0.1:9/mcp",
        policy=UrlPolicy(restricted=True, allowlist=("127.0.0.0/8",)),
        resolver=_never_resolve,
    )
    assert pinned == PinnedDestination(host="127.0.0.1", port=9, ip="127.0.0.1")


async def test_ipv4_mapped_unwrapped_for_pinning() -> None:
    pinned = await validate_outbound_url(
        "https://[::ffff:169.254.169.254]/mcp",
        policy=UrlPolicy(restricted=True, allowlist=("169.254.0.0/16",)),
        resolver=_never_resolve,
    )
    assert pinned.ip == "169.254.169.254"


async def test_unrestricted_skips_dns() -> None:
    pinned = await validate_outbound_url(
        "https://owned.example.com/mcp",
        policy=UrlPolicy(restricted=False),
        resolver=_never_resolve,
    )
    assert pinned == PinnedDestination(host="owned.example.com", port=443, ip=None)


async def test_redirect_hop_limit_enforced() -> None:
    with pytest.raises(UrlPolicyError, match="redirect limit"):
        await admit_redirect(
            "https://ok.example.com/x",
            policy=UrlPolicy(),
            hop=MAX_REDIRECT_HOPS + 1,
        )


async def test_redirect_target_validated_like_fresh_url() -> None:
    with pytest.raises(UrlPolicyError, match="blocked"):
        await admit_redirect(
            "http://127.0.0.1/x",
            policy=UrlPolicy(restricted=True),
            hop=1,
            resolver=_resolver([]),
        )
    pinned = await admit_redirect(
        "https://ok.example.com/x",
        policy=UrlPolicy(restricted=True),
        hop=1,
        resolver=_resolver(["93.184.216.34"]),
    )
    assert pinned.ip == "93.184.216.34"
