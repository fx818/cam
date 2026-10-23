"""Session pool keyed by (service_id, connection_id).

One MCP session per key, created on first use. Requests on a session run
concurrently; only establishment and teardown are serialized per key.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from cam.config import Settings
from cam.upstream.client import UpstreamSession
from cam.upstream.errors import map_exception, redact
from cam.upstream.types import ConnectionTarget

logger = logging.getLogger(__name__)

_REAP_INTERVAL_CAP_S = 30.0


class _PoolEntry:
    __slots__ = ("fingerprint", "last_used", "session")

    def __init__(self, session: UpstreamSession, fingerprint: tuple[object, ...]) -> None:
        self.session = session
        self.fingerprint = fingerprint
        self.last_used = time.monotonic()


def _target_fingerprint(target: ConnectionTarget) -> tuple[object, ...]:
    """The connection-relevant identity of a target (R18).

    Excludes `message_handler` (a fresh closure per call) but includes the
    transport, URL, credential headers and the URL policy. A pooled session is
    only reused while every one of those still matches, so a reused session can
    never keep serving under a URL-policy decision that has since changed.
    """
    return (
        target.transport,
        target.url,
        tuple(sorted((str(name), str(value)) for name, value in target.headers.items())),
        target.url_policy,
    )


class SessionPool:
    """Pools `UpstreamSession`s across concurrent gateway requests."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        idle_timeout_s: float | None = None,
        call_timeout_s: float | None = None,
    ) -> None:
        if settings is not None:
            resolved_idle = (
                idle_timeout_s if idle_timeout_s is not None else settings.session_idle_timeout_s
            )
            resolved_call = (
                call_timeout_s if call_timeout_s is not None else settings.default_timeout_s
            )
        else:
            if idle_timeout_s is None or call_timeout_s is None:
                raise ValueError("provide settings= or both idle_timeout_s and call_timeout_s")
            resolved_idle = idle_timeout_s
            resolved_call = call_timeout_s
        if resolved_idle <= 0:
            raise ValueError("idle_timeout_s must be > 0")
        if resolved_call <= 0:
            raise ValueError("call_timeout_s must be > 0")
        self.idle_timeout_s = float(resolved_idle)
        self.call_timeout_s = float(resolved_call)
        self._entries: dict[tuple[str, str], _PoolEntry] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._reaper: asyncio.Task[None] | None = None

    async def session(self, target: ConnectionTarget) -> UpstreamSession:
        key = (target.service_id, target.connection_id)
        fingerprint = _target_fingerprint(target)
        entry = self._entries.get(key)
        if entry is not None and not entry.session.closed and entry.fingerprint == fingerprint:
            entry.last_used = time.monotonic()
            return entry.session
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._entries.get(key)
            if entry is not None and not entry.session.closed and entry.fingerprint == fingerprint:
                entry.last_used = time.monotonic()
                return entry.session
            # The stored session was established under a different URL policy /
            # URL / credential set than the current request needs: retire it
            # first, then re-establish so the CURRENT policy is enforced at the
            # boundary and the connection is pinned to the address this check
            # validated (R18).
            stale = self._entries.pop(key, None)
            if stale is not None:
                await stale.session.aclose()
            session = await self._open_with_retry(target)
            self._entries[key] = _PoolEntry(session, fingerprint)
            self._ensure_reaper()
        return session

    async def invalidate(self, target: ConnectionTarget) -> None:
        """Close and drop the pooled session for this connection now."""
        await self.invalidate_connection(target.service_id, target.connection_id)

    async def invalidate_connection(self, service_id: str, connection_id: str) -> None:
        """Close and drop the pooled session for (service, connection) now."""
        key = (service_id, connection_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._entries.pop(key, None)
        if entry is not None:
            await entry.session.aclose()

    def active_count(self) -> int:
        return sum(1 for entry in self._entries.values() if not entry.session.closed)

    async def reap_idle(self) -> int:
        """Close sessions idle past the timeout. Returns how many were closed."""
        now = time.monotonic()
        stale_keys = [
            key
            for key, entry in self._entries.items()
            if entry.session.in_flight == 0
            and (now - entry.last_used) >= self.idle_timeout_s
            and not entry.session.closed
        ]
        closed = 0
        for key in stale_keys:
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                entry = self._entries.get(key)
                if entry is None:
                    continue
                if entry.session.in_flight > 0:
                    continue
                if (time.monotonic() - entry.last_used) < self.idle_timeout_s:
                    continue
                self._entries.pop(key, None)
                await entry.session.aclose()
                closed += 1
        return closed

    async def aclose(self) -> None:
        reaper = self._reaper
        self._reaper = None
        if reaper is not None:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
        entries = list(self._entries.values())
        self._entries.clear()
        for entry in entries:
            await entry.session.aclose()

    async def _open_with_retry(self, target: ConnectionTarget) -> UpstreamSession:
        # Spec: establishing (or re-establishing) a session may reconnect once.
        try:
            return await self._open_session(target)
        except KeyboardInterrupt, SystemExit, asyncio.CancelledError:
            raise
        except BaseException as first_exc:
            if getattr(first_exc, "retries_exhausted", False):
                # The establish layer already spent the one permitted reconnect
                # (its legacy→auto mode fallback ran and also failed). Retrying
                # here would let one logical connect make up to four attempts
                # (R35: own the retry policy in exactly one layer).
                error, _dead = map_exception(first_exc, recent_statuses=())
                raise error from first_exc
            logger.debug(
                "upstream session establish failed for %s/%s; reconnecting once: %s",
                target.service_id,
                target.connection_id,
                redact(str(first_exc)),
            )
        try:
            return await self._open_session(target)
        except KeyboardInterrupt, SystemExit, asyncio.CancelledError:
            raise
        except BaseException as exc:
            error, _dead = map_exception(exc, recent_statuses=())
            raise error from exc

    async def _open_session(self, target: ConnectionTarget) -> UpstreamSession:
        session = UpstreamSession(target, call_timeout_s=self.call_timeout_s)
        await session.start()
        return session

    def _ensure_reaper(self) -> None:
        if self._reaper is not None and not self._reaper.done():
            return
        try:
            self._reaper = asyncio.create_task(self._reap_loop(), name="cam-session-reaper")
        except RuntimeError:  # pragma: no cover - no running loop in sync contexts
            self._reaper = None

    async def _reap_loop(self) -> None:
        interval = min(self.idle_timeout_s, _REAP_INTERVAL_CAP_S)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reap_idle()
            except Exception:  # pragma: no cover - reaper must not die
                logger.exception("session reaper failed")


def pool_key(target: ConnectionTarget) -> tuple[str, str]:
    return (target.service_id, target.connection_id)
