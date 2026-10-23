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
import contextvars
import logging
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import AsyncExitStack
from typing import Any, cast

import httpx2
from mcp import types as mcp_types
from mcp.client import Client, Transport
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from cam.upstream.errors import (
    UpstreamError,
    UpstreamRemoteError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
    map_exception,
    redact,
)
from cam.upstream.normalize import normalize_call_result
from cam.upstream.pinning import pinned_transport_httpx2
from cam.upstream.types import ConnectionTarget, MessageHandler, ToolCallOutcome, UpstreamTool
from cam.upstream.url import PinnedDestination, UrlPolicyError, admit_url, validate_outbound_url

logger = logging.getLogger(__name__)

# Mirrors mcp.shared._httpx_utils.create_mcp_http_client defaults without
# importing that private module.
_HTTP_CONNECT_TIMEOUT_S = 30.0
_HTTP_READ_TIMEOUT_S = 300.0

# Discovery bounds (R36): a hostile or buggy upstream cannot make `list_tools`
# page forever or return an unbounded catalog.
MAX_TOOL_PAGES = 20
MAX_TOOLS = 1000

# Per-request HTTP-status capture (R35). The recorder appends to the sink in
# the current call's context, so concurrent `list_tools`/`call_tool` calls on
# one session never drain or misattribute each other's 401/403/429/5xx codes.
_status_ctx: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "cam_upstream_statuses", default=None
)


def _default_timeout() -> httpx2.Timeout:
    return httpx2.Timeout(_HTTP_CONNECT_TIMEOUT_S, read=_HTTP_READ_TIMEOUT_S)


def _status_recorder(fallback: list[int]) -> Callable[[httpx2.Response], Any]:
    async def record(response: httpx2.Response) -> None:
        sink = _status_ctx.get()
        (sink if sink is not None else fallback).append(response.status_code)

    return record


@contextlib.contextmanager
def _capture_statuses(fallback: list[int]) -> Generator[list[int]]:
    """Route this call's HTTP statuses to a fresh per-call sink."""
    del fallback  # kept for signature symmetry with the recorder's fallback
    sink: list[int] = []
    token = _status_ctx.set(sink)
    try:
        yield sink
    finally:
        _status_ctx.reset(token)


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
        kwargs["transport"] = pinned_transport_httpx2(pinned.host, pinned.ip)
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
        self._stream_channel = False

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

    @property
    def stream_channel(self) -> bool:
        """Whether the session holds the GET SSE notification channel.

        False means establishment fell back to auto mode (no GET stream), so
        the upstream cannot push `notifications/tools/list_changed` on this
        session; scheduled resync covers the drift.
        """
        return self._stream_channel

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
            raise UpstreamUnavailableError(
                f"URL policy rejected destination: {redact(str(exc))}"
            ) from exc

    async def start(self, *, timeout_s: float | None = None) -> None:
        if self._client is not None or self._owner is not None:
            raise RuntimeError("session already started")
        await self._validate_destination()
        connect_timeout = timeout_s if timeout_s is not None else self._call_timeout_s
        self._statuses.clear()
        self._closed = False
        self._start_error = None
        self._stream_channel = False
        try:
            await self._establish(connect_timeout, stream_channel=True)
        except UpstreamTimeoutError as first_error:
            # ONE establish reconnect (spec: "may reconnect once"). Some upstreams
            # answer the POST initialize quickly but their GET SSE stream never
            # produces bytes (a proxy holds it open), so the legacy establish
            # times out even though tools/call would work. Tear the half-open
            # session down and re-establish once in auto mode: no GET channel,
            # therefore no list_changed notifications on this session (the
            # scheduled resync covers that). Never runs again after this:
            # under a TimeoutError from the first attempt only, and never after
            # tools/call — the pool still creates a single session per key
            # because this fallback lives inside the same start() call.
            logger.warning(
                "legacy establish timed out for %s (%s); retrying once in auto "
                "mode without the GET SSE channel",
                self._target.service_id,
                redact(first_error.message or "no detail"),
            )
            try:
                await self._establish(connect_timeout, stream_channel=False)
            except UpstreamError as fallback_error:
                # The mode fallback already spent the one reconnect allowed for
                # this establish; tell the pool not to run its own retry too, or
                # a single logical connect becomes up to four attempts (R35).
                fallback_error.retries_exhausted = True
                raise
        self.touch()

    async def _establish(self, connect_timeout: float, *, stream_channel: bool) -> None:
        """Run one owner task until startup finishes (or raise its mapped error)."""
        self._client = None
        self._closed = False
        self._statuses.clear()
        self._start_error = None
        self._ready = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._owner = asyncio.create_task(
            self._run(connect_timeout, stream_channel),
            name=f"cam-upstream:{self._target.service_id}",
        )
        try:
            await self._ready.wait()
        except BaseException:
            # Cancelled (or otherwise interrupted) while waiting for the owner
            # to finish starting: tear the owner task down so it cannot outlive
            # this call still holding a transport/half-open connection (R35).
            await self._abort_owner()
            raise
        if self._start_error is not None:
            error = self._start_error
            self._start_error = None
            await self._reap_owner()
            raise error

    async def list_tools(self, *, timeout_s: float | None = None) -> list[UpstreamTool]:
        client = self._require_client()
        budget = timeout_s if timeout_s is not None else self._call_timeout_s
        self._in_flight += 1
        try:
            with _capture_statuses(self._statuses) as statuses:
                try:
                    tools: list[UpstreamTool] = []
                    cursor: str | None = None
                    pages = 0
                    async with asyncio.timeout(budget):
                        while True:
                            page = await client.list_tools(cursor=cursor, cache_mode="bypass")
                            pages += 1
                            for tool in page.tools:
                                tools.append(
                                    UpstreamTool(
                                        name=tool.name,
                                        description=tool.description or "",
                                        input_schema=dict(tool.input_schema),
                                    )
                                )
                            if pages > MAX_TOOL_PAGES or len(tools) > MAX_TOOLS:
                                raise UpstreamRemoteError(
                                    "upstream tool catalog exceeded CAM discovery limits"
                                )
                            cursor = page.next_cursor
                            if not cursor:
                                break
                    self.touch()
                    return tools
                except BaseException as exc:
                    self._raise_mapped(exc, statuses)
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
        self._in_flight += 1
        try:
            with _capture_statuses(self._statuses) as statuses:
                try:
                    async with asyncio.timeout(budget):
                        result = await client.call_tool(
                            name, arguments, read_timeout_seconds=budget
                        )
                    self.touch()
                    return normalize_call_result(result)
                except BaseException as exc:
                    self._raise_mapped(exc, statuses)
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

    async def _run(self, connect_timeout: float, stream_channel: bool) -> None:
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
                # stream_channel=True → "legacy": the standard initialize
                # handshake plus the GET SSE channel that servers use to push
                # notifications/tools/list_changed. stream_channel=False →
                # "auto": server/discover negotiation with no GET channel,
                # used only as the one-time establish fallback when the GET
                # stream hangs.
                mode="legacy" if stream_channel else "auto",
            )
            async with asyncio.timeout(connect_timeout):
                await stack.enter_async_context(client)
            self._client = client
            self._server_info = client.server_info
            self._stream_channel = stream_channel
            if not stream_channel:
                logger.warning(
                    "upstream %s established without stream channel; "
                    "notifications/tools/list_changed will not arrive on "
                    "this session",
                    self._target.service_id,
                )
            ready.set()
            # Establish statuses were only needed to classify a failed start;
            # clear them so the session-wide fallback sink cannot grow for the
            # life of a long-lived session (calls use per-call sinks, R35).
            statuses.clear()
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
                    # str(exc) can embed the request URL with credential query
                    # params; never log it raw (R04).
                    logger.debug("upstream session owner failed: %s", redact(str(exc)))
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

    async def _abort_owner(self) -> None:
        """Cancel the in-flight owner task and wait for it to unwind (R35).

        Used on the cancellation/interrupt path while `start()` is waiting for
        `_ready`, so a cancelled establish never leaves a live transport or a
        half-open upstream connection behind.
        """
        owner = self._owner
        self._owner = None
        self._client = None
        self._closed = True
        shutdown = self._shutdown
        if shutdown is not None:
            shutdown.set()
        if owner is None:
            return
        owner.cancel()
        with contextlib.suppress(BaseException):
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

    def _raise_mapped(self, exc: BaseException, statuses: Sequence[int]) -> None:
        """Map `exc` to an `UpstreamError` and raise it (never returns).

        `statuses` holds only the HTTP status codes observed for *this* call,
        so a concurrent call's 401/429/5xx can never be misattributed here
        (R35).
        """
        if isinstance(exc, UpstreamError):
            raise exc
        if type(exc).__name__ in {"CancelledError", "KeyboardInterrupt", "SystemExit"}:
            raise exc
        error, dead = map_exception(exc, recent_statuses=statuses)
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
