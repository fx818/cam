"""IP pinning for outbound HTTP: connect to a validated address, keep SNI.

The network backend swaps only the TCP destination; TLS/SNI still uses the
original hostname, so certificates validate against the name the URL carried.
Separate backends cover httpx/httpcore (REST executor) and httpx2/httpcore2
(MCP client transport).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpcore
import httpcore2
import httpx
import httpx2
from httpcore._backends.auto import AutoBackend as HttpAutoBackend
from httpcore2._backends.auto import AutoBackend as HttpCore2AutoBackend

_SocketOption = (
    tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
)


class _PinnedHttpBackend(HttpAutoBackend):
    def __init__(self, hostname: str, pinned_ip: str) -> None:
        super().__init__()
        self._hostname = hostname
        self._pinned_ip = pinned_ip

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - backend interface parameter
        local_address: str | None = None,
        socket_options: Iterable[_SocketOption] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        target = self._pinned_ip if host == self._hostname else host
        return await super().connect_tcp(
            target,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class _PinnedHttp2Backend(HttpCore2AutoBackend):
    def __init__(self, hostname: str, pinned_ip: str) -> None:
        super().__init__()
        self._hostname = hostname
        self._pinned_ip = pinned_ip

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - backend interface parameter
        local_address: str | None = None,
        socket_options: Iterable[_SocketOption] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        target = self._pinned_ip if host == self._hostname else host
        return await super().connect_tcp(
            target,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


def pinned_transport(hostname: str, pinned_ip: str) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport()
    pool: Any = transport._pool  # pyright: ignore[reportPrivateUsage]
    pool._network_backend = _PinnedHttpBackend(hostname, pinned_ip)  # pyright: ignore[reportPrivateUsage]
    return transport


def pinned_transport_httpx2(hostname: str, pinned_ip: str) -> httpx2.AsyncHTTPTransport:
    transport = httpx2.AsyncHTTPTransport()
    pool: Any = transport._pool  # pyright: ignore[reportPrivateUsage]
    pool._network_backend = _PinnedHttp2Backend(hostname, pinned_ip)  # pyright: ignore[reportPrivateUsage]
    return transport
