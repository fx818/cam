"""One MCP session bound to a single upstream connection.

Only this module (and the pool that owns it) opens outbound MCP connections.

The MCP SDK's transports install anyio task groups that must be entered and
exited in the same asyncio task. Each session therefore runs its own owner
task: enter and exit happen there, while `list_tools` / `call_tool` may be
called from any task (the SDK's streams are task-safe).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterable
from contextlib import AsyncExitStack
from typing import Any, cast

import httpcore2
import httpx2
from httpcore2._backends.auto import AutoBackend
from mcp import types as mcp_types
from mcp.client import Client, Transport
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from cam.upstream.errors import UpstreamError, UpstreamUnavailableError, map_exception
from cam.upstream.normalize import normalize_call_result
from cam.upstream.types import ConnectionTarget, MessageHandler, ToolCallOutcome, UpstreamTool
from cam.upstream.url import PinnedDestination, UrlPolicyError, admit_url, validate_outbound_url

logger = logging.getLogger(__name__)

# Mirrors mcp.shared._httpx_utils.create_mcp_http_client defaults without
# importing that private module.
_HTTP_CONNECT_TIMEOUT_S = 30.0
_HTTP_READ_TIMEOUT_S = 300.0

_SocketOption = (
    tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
)


def _default_timeout() -> httpx2.Timeout:
    return httpx2.Timeout(_HTTP_CONNECT_TIMEOUT_S, read=_HTTP_READ_TIMEOUT_S)


def _status_recorder(statuses: list[int]) -> Callable[[httpx2.Response], Any]:
    async def record(response: httpx2.Response) -> None:
        statuses.append(response.status_code)

    return record


class _PinnedNetworkBackend(AutoBackend):
    """Sends TCP connects for the original hostname to the validated IP.

    TLS/SNI still uses the original hostname (httpcore2 passes the origin
    host to `start_tls`), so only the socket destination is swapped.
    """

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


def _pinned_transport(hostname: str, pinned_ip: str) -> httpx2.AsyncHTTPTransport:
    transport = httpx2.AsyncHTTPTransport()
    pool = transport._pool  # pyright: ignore[reportPrivateUsage]
    pool._network_backend = _PinnedNetworkBackend(hostname, pinned_ip)  # pyright: ignore[reportPrivateUsage]
    return transport


def _make_http_client(
    headers: dict[str, str] | None,
    timeout: httpx2.Timeout | None,
    statuses: list[int],
    *,
    pinned: PinnedDestination | None = None,
) -> httpx2.AsyncClient:
    kwargs: dict[str, Any] = {
        "timeout": timeout or _default_timeout(),
        "follow_redirects": False,
    }
    if headers:
        kwargs["headers"] = headers
    kwargs["event_hooks"] = {"response": [_status_recorder(statuses)]}
    if pinned is not None and pinned.ip is not None:
        kwargs["transport"] = _pinned_transport(pinned.host, pinned.ip)
    return httpx2.AsyncClient(**kwargs)


def _drain(statuses: list[int]) -> list[int]:
    drained = list(statuses)
    statuses.clear()
    return drained


class UpstreamSession:
    """A live MCP session: `tools/list` and `tools/call` with typed errors."""

    def __init__(
        self,
        target: ConnectionTarget,
        *,
        call_timeout_s: float,
    ) -> None:
        admit_url(target.url)
        self._target = target
        self._call_timeout_s = call_timeout_s
        self._client: Client | None = None
        self._statuses: list[int] = []
        self._closed = False
        self._in_flight = 0
        self._last_used = time.monotonic()
        self._server_info: mcp_types.Implementation | None = None
        self._owner: asyncio.Task[None] | None = None
        self._ready: asyncio.Event | None = None
        self._shutdown: asyncio.Event | None = None
        self._start_error: BaseException | None = None
        self._pinned: PinnedDestination | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def healthy(self) -> bool:
        return not self._closed and self._client is not None

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def last_used(self) -> float:
        return self._last_used

    @property
    def server_info(self) -> mcp_types.Implementation | None:
        return self._server_info

    def touch(self) -> None:
        self._last_used = time.monotonic()

    async def _validate_destination(self) -> None:
        policy = self._target.url_policy
        self._pinned = None
        if policy is None or not policy.restricted:
            return
        try:
            self._pinned = await validate_outbound_url(self._target.url, policy=policy)
        except UrlPolicyError as exc:
            raise UpstreamUnavailableError(f"URL policy rejected destination: {exc}") from exc

    async def start(self, *, timeout_s: float | None = None) -> None:
        if self._client is not None or self._owner is not None:
            raise RuntimeError("session already started")
        await self._validate_destination()
        connect_timeout = timeout_s if timeout_s is not None else self._call_timeout_s
        self._statuses.clear()
        self._closed = False
        self._start_error = None
        self._ready = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._owner = asyncio.create_task(
            self._run(connect_timeout),
            name=f"cam-upstream:{self._target.service_id}",
        )
        await self._ready.wait()
        if self._start_error is not None:
            error = self._start_error
            self._start_error = None
            await self._reap_owner()
            raise error
        self.touch()

    async def list_tools(self, *, timeout_s: float | None = None) -> list[UpstreamTool]:
        client = self._require_client()
        budget = timeout_s if timeout_s is not None else self._call_timeout_s
        self._statuses.clear()
        self._in_flight += 1
        try:
            tools: list[UpstreamTool] = []
            cursor: str | None = None
            async with asyncio.timeout(budget):
                while True:
                    page = await client.list_tools(cursor=cursor, cache_mode="bypass")
                    for tool in page.tools:
                        tools.append(
                            UpstreamTool(
                                name=tool.name,
                                description=tool.description or "",
                                input_schema=dict(tool.input_schema),
                            )
                        )
                    cursor = page.next_cursor
                    if not cursor:
                        break
            self.touch()
            return tools
        except BaseException as exc:
            self._raise_mapped(exc)
            raise  # unreachable; _raise_mapped always raises
        finally:
            self._in_flight -= 1

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> ToolCallOutcome:
        client = self._require_client()
        budget = timeout_s if timeout_s is not None else self._call_timeout_s
        self._statuses.clear()
        self._in_flight += 1
        try:
            async with asyncio.timeout(budget):
                result = await client.call_tool(name, arguments, read_timeout_seconds=budget)
            self.touch()
            return normalize_call_result(result)
        except BaseException as exc:
            self._raise_mapped(exc)
            raise  # unreachable; _raise_mapped always raises
        finally:
            self._in_flight -= 1

    async def aclose(self) -> None:
        if self._closed and self._owner is None:
            return
        self._closed = True
        shutdown = self._shutdown
        if shutdown is not None:
            shutdown.set()
        await self._reap_owner()

    async def _run(self, connect_timeout: float) -> None:
        ready = self._ready
        shutdown = self._shutdown
        if ready is None or shutdown is None:  # pragma: no cover - set in start()
            raise RuntimeError("session owner started without events")
        statuses = self._statuses
        statuses.clear()
        stack = AsyncExitStack()
        try:
            transport = self._build_transport(stack, statuses)
            handler: MessageHandler | None = self._target.message_handler
            client = Client(
                transport,
                message_handler=handler,  # type: ignore[arg-type]
                read_timeout_seconds=connect_timeout,
                # "legacy" keeps the standard initialize + GET SSE channel, which
                # is what servers use to push notifications/tools/list_changed.
                # mode="auto" negotiates via server/discover without that channel.
                mode="legacy",
            )
            async with asyncio.timeout(connect_timeout):
                await stack.enter_async_context(client)
            self._client = client
            self._server_info = client.server_info
            ready.set()
            await shutdown.wait()
        except BaseException as exc:
            if not ready.is_set():
                error, dead = map_exception(exc, recent_statuses=_drain(statuses))
                if dead:
                    self._closed = True
                self._start_error = error
                ready.set()
            else:
                # Post-start failure while waiting on shutdown (or cancel on abort).
                if type(exc).__name__ not in {"CancelledError"}:
                    logger.debug("upstream session owner failed", exc_info=exc)
                self._closed = True
        finally:
            self._client = None
            self._closed = True
            with contextlib.suppress(Exception):
                await stack.aclose()
            if not ready.is_set():
                ready.set()

    async def _reap_owner(self) -> None:
        owner = self._owner
        self._owner = None
        self._client = None
        if owner is None:
            return
        with contextlib.suppress(Exception):
            await owner

    def _build_transport(self, stack: AsyncExitStack, statuses: list[int]) -> Transport:
        target = self._target
        if target.transport == "streamable-http":
            headers = dict(target.headers) or None
            http_client = _make_http_client(headers, None, statuses, pinned=self._pinned)
            # We own the client: close it after the transport unwinds.
            stack.push_async_callback(http_client.aclose)
            return cast(
                "Transport",
                streamable_http_client(target.url, http_client=http_client),
            )
        factory = _sse_http_client_factory(statuses, self._pinned)
        return cast(
            "Transport",
            sse_client(
                target.url,
                headers=dict(target.headers) or None,
                httpx_client_factory=factory,  # type: ignore[arg-type]
            ),
        )

    def _require_client(self) -> Client:
        if self._closed or self._client is None:
            raise RuntimeError("session is closed")
        return self._client

    def _raise_mapped(self, exc: BaseException) -> None:
        """Map `exc` to an `UpstreamError` and raise it (never returns)."""
        if isinstance(exc, UpstreamError):
            raise exc
        if type(exc).__name__ in {"CancelledError", "KeyboardInterrupt", "SystemExit"}:
            raise exc
        error, dead = map_exception(exc, recent_statuses=_drain(self._statuses))
        if dead:
            self._closed = True
        raise error from exc


def _sse_http_client_factory(
    statuses: list[int],
    pinned: PinnedDestination | None,
) -> Callable[..., httpx2.AsyncClient]:
    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
    ) -> httpx2.AsyncClient:
        client = _make_http_client(headers, timeout, statuses, pinned=pinned)
        if auth is not None:
            client.auth = auth
        return client

    return factory
