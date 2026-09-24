"""URL admission for every outbound upstream connection.

Personal (restricted) destinations must not reach loopback, private,
link-local or otherwise internal addresses. `admit_url` enforces the static
rules (scheme, shape, literal IPs, reserved hostnames); `validate_outbound_url`
adds DNS resolution, validates every resolved address, and returns the address
to pin the connection to. Org allowlist patterns (exact hostname or CIDR)
exempt matching destinations. Redirects are never auto-followed by HTTP
clients; each hop is validated through `admit_redirect` with a hard hop limit.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlparse

MAX_REDIRECT_HOPS = 3

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata.google.internal"})
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)
Resolver = Callable[[str, int], Awaitable[list[str]]]


class UrlPolicyError(ValueError):
    """A destination violates the outbound URL policy."""


@dataclass(frozen=True, slots=True)
class UrlPolicy:
    """Outbound policy for one connection: restricted checks plus allowlist."""

    restricted: bool = False
    allowlist: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PinnedDestination:
    """Validated destination: the host the client speaks and the IP to pin."""

    host: str
    port: int
    ip: str | None


def _parse_ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(ip in net for net in _BLOCKED_NETWORKS)


def _allowlisted_host(host: str, allowlist: tuple[str, ...]) -> bool:
    return host in allowlist


def _allowlisted_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, allowlist: tuple[str, ...]
) -> bool:
    for pattern in allowlist:
        try:
            network = ipaddress.ip_network(pattern, strict=False)
        except ValueError:
            continue
        if ip in network:
            return True
    return False


async def _default_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in addresses:
            addresses.append(addr)
    return addresses


def admit_url(url: str, *, restricted: bool = False, allowlist: tuple[str, ...] = ()) -> str:
    """Validate `url` for outbound use and return it unchanged.

    Always enforces absolute http(s) shape with a host and no embedded
    credentials. When `restricted` (personal-tier destinations), also rejects
    reserved hostnames and internal/reserved literal IPs unless the allowlist
    exempts them. Raises `ValueError` on any violation.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"unsupported URL scheme {parsed.scheme!r}; expected http or https")
    if not parsed.hostname:
        raise ValueError("URL must include a host")
    if parsed.username or parsed.password:
        raise ValueError("URL must not embed credentials")
    if not restricted:
        return url
    host = parsed.hostname
    if _allowlisted_host(host, allowlist):
        return url
    if host in _BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        raise UrlPolicyError(f"URL host {host!r} is blocked by URL policy for personal services")
    literal = _parse_ip(host)
    if literal is not None and _is_blocked_ip(literal) and not _allowlisted_ip(literal, allowlist):
        raise UrlPolicyError(f"URL host {host!r} is a blocked address for personal services")
    return url


async def validate_outbound_url(
    url: str, *, policy: UrlPolicy, resolver: Resolver = _default_resolver
) -> PinnedDestination:
    """Validate `url` under `policy`, resolving DNS when restricted.

    Every resolved address must pass the blocked-range check (unless
    allowlisted); the first passing address is returned for pinning. Hosts
    on the allowlist skip resolution entirely and are not pinned.
    """
    admit_url(url, restricted=policy.restricted, allowlist=policy.allowlist)
    parsed = urlparse(url)
    host = parsed.hostname
    if host is None:
        raise ValueError("URL must include a host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not policy.restricted:
        return PinnedDestination(host=host, port=port, ip=None)
    if _allowlisted_host(host, policy.allowlist):
        return PinnedDestination(host=host, port=port, ip=None)
    literal = _parse_ip(host)
    if literal is not None:
        return PinnedDestination(host=host, port=port, ip=str(literal))
    try:
        addresses = await resolver(host, port)
    except (socket.gaierror, OSError) as exc:
        raise UrlPolicyError(f"cannot resolve URL host {host!r}") from exc
    if not addresses:
        raise UrlPolicyError(f"URL host {host!r} resolved to no addresses")
    for address in addresses:
        ip = _parse_ip(address)
        if ip is None:
            raise UrlPolicyError(f"URL host {host!r} resolved to invalid address {address!r}")
        if _is_blocked_ip(ip) and not _allowlisted_ip(ip, policy.allowlist):
            raise UrlPolicyError(f"URL host {host!r} resolves to blocked address {address}")
    pinned = _parse_ip(addresses[0])
    if pinned is None:
        raise UrlPolicyError(f"URL host {host!r} resolved to invalid address {addresses[0]!r}")
    return PinnedDestination(host=host, port=port, ip=str(pinned))


async def admit_redirect(
    location: str,
    *,
    policy: UrlPolicy,
    hop: int,
    resolver: Resolver = _default_resolver,
) -> PinnedDestination:
    """Validate one redirect hop: same rules as a fresh outbound URL, max hops."""
    if hop > MAX_REDIRECT_HOPS:
        raise UrlPolicyError(f"redirect limit of {MAX_REDIRECT_HOPS} exceeded")
    return await validate_outbound_url(location, policy=policy, resolver=resolver)
