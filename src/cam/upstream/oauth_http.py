"""Outbound HTTP helpers for OAuth discovery, DCR and token endpoints.

Only this module (and the other `cam.upstream` clients) opens outbound
connections. Personal destinations go through the same URL policy as tool
calls. Responses are never logged with bodies that could hold tokens.
"""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import urlparse

import httpx

from cam.upstream.errors import (
    UpstreamRemoteError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from cam.upstream.pinning import pinned_transport
from cam.upstream.url import PinnedDestination, UrlPolicy, admit_url, validate_outbound_url


async def _pin(url: str, policy: UrlPolicy) -> PinnedDestination | None:
    try:
        if not policy.restricted:
            admit_url(url)
            return None
        pinned = await validate_outbound_url(url, policy=policy)
    except ValueError as exc:
        raise UpstreamUnavailableError(f"URL policy rejected destination: {exc}") from exc
    return pinned if pinned.ip is not None else None


async def fetch_json(
    url: str,
    *,
    timeout_s: float = 15.0,
    url_policy: UrlPolicy | None = None,
) -> dict[str, Any]:
    policy = url_policy if url_policy is not None else UrlPolicy()
    pinned = await _pin(url, policy)
    transport = (
        pinned_transport(urlparse(url).hostname or "", pinned.ip) if pinned and pinned.ip else None
    )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
            transport=transport,
        ) as client:
            response = await client.get(url)
    except httpx.TimeoutException as exc:
        raise UpstreamTimeoutError(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise UpstreamUnavailableError(str(exc)) from exc
    if response.status_code in (301, 302, 303, 307, 308):
        raise UpstreamRemoteError("unexpected redirect while fetching OAuth metadata")
    if response.status_code >= 400:
        raise UpstreamRemoteError(f"OAuth metadata endpoint returned {response.status_code}")
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise UpstreamRemoteError("OAuth metadata endpoint did not return JSON") from exc
    if not isinstance(payload, dict):
        raise UpstreamRemoteError("OAuth metadata endpoint did not return a JSON object")
    return cast("dict[str, Any]", payload)


async def post_form(
    url: str,
    data: dict[str, str],
    *,
    timeout_s: float = 15.0,
    url_policy: UrlPolicy | None = None,
    basic_auth: tuple[str, str] | None = None,
) -> dict[str, Any]:
    policy = url_policy if url_policy is not None else UrlPolicy()
    pinned = await _pin(url, policy)
    transport = (
        pinned_transport(urlparse(url).hostname or "", pinned.ip) if pinned and pinned.ip else None
    )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
            transport=transport,
        ) as client:
            kwargs: dict[str, Any] = {}
            if basic_auth is not None:
                kwargs["auth"] = basic_auth
            response = await client.post(url, data=data, **kwargs)
    except httpx.TimeoutException as exc:
        raise UpstreamTimeoutError(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise UpstreamUnavailableError(str(exc)) from exc
    if response.status_code in (301, 302, 303, 307, 308):
        raise UpstreamRemoteError("unexpected redirect from OAuth token endpoint")
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise UpstreamRemoteError("OAuth token endpoint did not return JSON") from exc
    if response.status_code >= 400:
        err_obj = cast("dict[str, Any]", payload) if isinstance(payload, dict) else {}
        detail = str(err_obj.get("error") or response.status_code)
        raise UpstreamRemoteError(f"OAuth token endpoint rejected the request: {detail}")
    if not isinstance(payload, dict):
        raise UpstreamRemoteError("OAuth token endpoint did not return a JSON object")
    return cast("dict[str, Any]", payload)


async def post_json(
    url: str,
    body: dict[str, Any],
    *,
    timeout_s: float = 15.0,
    url_policy: UrlPolicy | None = None,
) -> dict[str, Any]:
    policy = url_policy if url_policy is not None else UrlPolicy()
    pinned = await _pin(url, policy)
    transport = (
        pinned_transport(urlparse(url).hostname or "", pinned.ip) if pinned and pinned.ip else None
    )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
            transport=transport,
        ) as client:
            response = await client.post(url, json=body)
    except httpx.TimeoutException as exc:
        raise UpstreamTimeoutError(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise UpstreamUnavailableError(str(exc)) from exc
    if response.status_code in (301, 302, 303, 307, 308):
        raise UpstreamRemoteError("unexpected redirect from OAuth registration endpoint")
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise UpstreamRemoteError("OAuth registration endpoint did not return JSON") from exc
    if response.status_code >= 400:
        raise UpstreamRemoteError(f"OAuth registration failed with {response.status_code}")
    if not isinstance(payload, dict):
        raise UpstreamRemoteError("OAuth registration did not return a JSON object")
    return cast("dict[str, Any]", payload)
