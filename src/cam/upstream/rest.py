"""REST executor: build an HTTP request from a tool's `rest_binding`, inject
the credential, and normalize the response into the CAM content shape.

Only this module opens outbound REST connections. Redirects are never
auto-followed: each hop is validated through the URL policy (max 3 hops) and
the credential is re-injected. Status codes map to the typed upstream errors
the invoke pipeline already turns into agent-facing envelopes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import quote, urljoin, urlparse

import httpx

from cam.upstream.errors import (
    UpstreamAuthError,
    UpstreamInternalError,
    UpstreamRateLimitedError,
    UpstreamRemoteError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from cam.upstream.pinning import pinned_transport
from cam.upstream.types import TextBlock, ToolCallOutcome
from cam.upstream.url import (
    MAX_REDIRECT_HOPS,
    PinnedDestination,
    UrlPolicy,
    admit_redirect,
    admit_url,
    validate_outbound_url,
)

BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
PARAM_LOCATIONS = frozenset({"path", "query", "header", "body"})
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


class RestBindingError(ValueError):
    """The stored `rest_binding` cannot produce a request."""


@dataclass(frozen=True, slots=True)
class RestRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict[str, str])
    params: dict[str, str] = field(default_factory=dict[str, str])
    json_body: dict[str, Any] | None = None


def _binding_method(binding: Mapping[str, Any]) -> str:
    method = str(binding.get("method") or "").strip().upper()
    if method not in HTTP_METHODS:
        raise RestBindingError(f"unsupported REST method {method!r}")
    return method


def _binding_path(binding: Mapping[str, Any]) -> str:
    path = str(binding.get("path") or "").strip()
    if not path:
        raise RestBindingError("rest_binding is missing its path")
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        raise RestBindingError("rest_binding path must be relative to the service URL")
    return path


def _param_locations(binding: Mapping[str, Any]) -> dict[str, str]:
    raw = binding.get("params")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise RestBindingError("rest_binding params must be an object")
    locations: dict[str, str] = {}
    for name, location in cast("Mapping[str, Any]", raw).items():
        resolved = str(location)
        if resolved not in PARAM_LOCATIONS:
            raise RestBindingError(
                f"parameter {name!r} has unknown location {location!r}; "
                f"expected one of {sorted(PARAM_LOCATIONS)}"
            )
        locations[str(name)] = resolved
    return locations


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def build_rest_request(
    base_url: str,
    binding: Mapping[str, Any],
    args: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    query: Mapping[str, str] | None = None,
) -> RestRequest:
    admit_url(base_url)
    method = _binding_method(binding)
    path = _binding_path(binding)
    locations = _param_locations(binding)
    credential_headers = dict(headers or {})
    credential_query = dict(query or {})

    arg_headers: dict[str, str] = {}
    arg_query: dict[str, str] = {}
    body: dict[str, Any] = {}

    for name, value in args.items():
        location = locations.get(name)
        if location is None:
            location = "body" if method in BODY_METHODS else "query"
        if location == "path":
            continue
        if location == "header":
            arg_headers[name] = _stringify(value)
        elif location == "query":
            arg_query[name] = _stringify(value)
        else:
            body[name] = value

    resolved_path = path
    index = 0
    while True:
        start = resolved_path.find("{", index)
        if start < 0:
            break
        end = resolved_path.find("}", start)
        if end < 0:
            raise RestBindingError(f"unterminated path placeholder in {path!r}")
        placeholder = resolved_path[start + 1 : end]
        if placeholder not in args:
            raise RestBindingError(f"missing path parameter {placeholder!r}")
        token = quote(_stringify(args[placeholder]), safe="")
        resolved_path = resolved_path[:start] + token + resolved_path[end + 1 :]
        index = start + len(token)
    if "{" in resolved_path or "}" in resolved_path:
        raise RestBindingError(f"malformed path placeholder in {path!r}")

    base = base_url.rstrip("/") + "/"
    url = urljoin(base, resolved_path.lstrip("/"))
    merged_headers = {**arg_headers, **credential_headers}
    merged_query = {**arg_query, **credential_query}
    return RestRequest(
        method=method,
        url=url,
        headers=merged_headers,
        params=merged_query,
        json_body=body or None,
    )


def normalize_rest_response(response: httpx.Response) -> ToolCallOutcome:
    text = response.text
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == "application/json" or content_type.endswith("+json"):
        try:
            data: Any = response.json()
        except ValueError:
            return ToolCallOutcome(content=(TextBlock(text=text),), structured=None, is_error=False)
        payload = json.dumps(data, ensure_ascii=False)
        structured = cast("dict[str, Any]", data) if isinstance(data, dict) else None
        return ToolCallOutcome(
            content=(TextBlock(text=payload),), structured=structured, is_error=False
        )
    try:
        parsed: Any = json.loads(text)
    except ValueError:
        return ToolCallOutcome(content=(TextBlock(text=text),), structured=None, is_error=False)
    payload = json.dumps(parsed, ensure_ascii=False)
    structured = cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else None
    return ToolCallOutcome(
        content=(TextBlock(text=payload),), structured=structured, is_error=False
    )


def _response_text(response: httpx.Response) -> str:
    try:
        text = response.text
    except Exception:
        return f"HTTP {response.status_code}"
    return text or f"HTTP {response.status_code}"


def _raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    text = _response_text(response)
    if status in (401, 403):
        raise UpstreamAuthError(text)
    if status == 429:
        raise UpstreamRateLimitedError(text)
    if status >= 500:
        raise UpstreamUnavailableError(text)
    raise UpstreamRemoteError(text)


async def _pin(url: str, policy: UrlPolicy) -> PinnedDestination | None:
    try:
        if not policy.restricted:
            admit_url(url)
            return None
        pinned = await validate_outbound_url(url, policy=policy)
    except ValueError as exc:
        raise UpstreamUnavailableError(f"URL policy rejected destination: {exc}") from exc
    return pinned if pinned.ip is not None else None


async def _send(
    request: RestRequest,
    *,
    timeout_s: float,
    policy: UrlPolicy,
) -> httpx.Response:
    url = request.url
    pinned = await _pin(url, policy)
    hop = 0
    while True:
        transport = (
            pinned_transport(urlparse(url).hostname or "", pinned.ip)
            if pinned is not None and pinned.ip is not None
            else None
        )
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_s),
                follow_redirects=False,
                transport=transport,
            ) as client:
                response = await client.request(
                    request.method,
                    url,
                    params=request.params or None,
                    json=request.json_body,
                    headers=request.headers or None,
                )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("location")
        if not location:
            return response
        hop += 1
        if hop > MAX_REDIRECT_HOPS:
            raise UpstreamRemoteError(f"redirect limit of {MAX_REDIRECT_HOPS} exceeded")
        next_url = urljoin(url, location)
        try:
            await admit_redirect(next_url, policy=policy, hop=hop)
        except ValueError as exc:
            raise UpstreamUnavailableError(f"URL policy rejected redirect: {exc}") from exc
        url = next_url
        pinned = await _pin(url, policy)


async def execute_rest(
    *,
    base_url: str,
    binding: Mapping[str, Any],
    args: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
    query: Mapping[str, str] | None = None,
    timeout_ms: int = 60000,
    url_policy: UrlPolicy | None = None,
) -> ToolCallOutcome:
    try:
        request = build_rest_request(base_url, binding, args, headers=headers, query=query)
    except RestBindingError as exc:
        raise UpstreamInternalError(str(exc)) from exc
    except ValueError as exc:
        raise UpstreamUnavailableError(str(exc)) from exc
    policy = url_policy if url_policy is not None else UrlPolicy()
    response = await _send(request, timeout_s=max(timeout_ms, 1) / 1000.0, policy=policy)
    _raise_for_status(response)
    return normalize_rest_response(response)


async def fetch_text(
    url: str,
    *,
    timeout_s: float = 15.0,
    url_policy: UrlPolicy | None = None,
) -> str:
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
        raise UpstreamRemoteError("unexpected redirect while fetching resource")
    _raise_for_status(response)
    return response.text
