"""REST executor: build an HTTP request from a tool's `rest_binding`, inject
the credential, and normalize the response into the CAM content shape.

Only this module opens outbound REST connections. Redirects are never
auto-followed: each hop is validated through the URL policy (max 3 hops),
pinned to the address it validated, and the credential is only ever sent to
the original origin. Status codes map to the typed upstream errors the invoke
pipeline already turns into agent-facing envelopes.
"""

from __future__ import annotations

import asyncio
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
    redact,
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
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_SAFE_METHODS = frozenset({"GET", "HEAD"})

# Hard ceiling on how many response bytes are buffered for one tool call /
# resource fetch (R36). Beyond it the body is truncated so a hostile or
# pathological upstream cannot exhaust process memory.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_FETCH_BYTES = 2 * 1024 * 1024


class RestBindingError(ValueError):
    """The stored `rest_binding` cannot produce a request."""


@dataclass(frozen=True, slots=True)
class RestRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict[str, str])
    params: dict[str, str] = field(default_factory=dict[str, str])
    json_body: dict[str, Any] | None = None
    # Names of the injected credential material (R05). These are stripped
    # before forwarding a redirect to a different origin.
    credential_header_names: frozenset[str] = frozenset()
    credential_param_names: frozenset[str] = frozenset()


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
        # The injected credential set: exactly the keys the pipeline passed in,
        # never caller-supplied arguments. Redirects drop these cross-origin.
        credential_header_names=frozenset(credential_headers),
        credential_param_names=frozenset(credential_query),
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
        raise UpstreamUnavailableError(
            f"URL policy rejected destination: {redact(str(exc))}"
        ) from exc
    return pinned if pinned.ip is not None else None


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    return (scheme, host, port)


def _redirect_method(method: str, status: int) -> tuple[str, bool]:
    """Return `(next_method, keep_body)` for one redirect hop.

    ``303`` always becomes ``GET`` with no body; ``301``/``302`` downgrade a
    mutating method to ``GET`` (never replaying the body) as browsers do;
    ``307``/``308`` preserve method and body for same-origin hops.
    """
    upper = method.upper()
    if status == 303:
        return ("HEAD" if upper == "HEAD" else "GET"), False
    if status in (301, 302):
        return (upper if upper in _SAFE_METHODS else "GET"), False
    return upper, True


async def _read_capped(response: httpx.Response, limit: int) -> tuple[bytes, bool]:
    """Read at most `limit` body bytes; returns `(data, truncated)`."""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in response.aiter_bytes():
        if total + len(chunk) > limit:
            remaining = limit - total
            if remaining > 0:
                chunks.append(chunk[:remaining])
                total += remaining
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), truncated


async def _send(
    request: RestRequest,
    *,
    timeout_s: float,
    policy: UrlPolicy,
) -> httpx.Response:
    original_origin = _origin(request.url)
    url = request.url
    method = request.method
    body = request.json_body
    headers: dict[str, str] = dict(request.headers)
    params: dict[str, str] = dict(request.params)
    pinned = await _pin(url, policy)
    per_phase = httpx.Timeout(timeout_s)
    hop = 0
    while True:
        transport = (
            pinned_transport(urlparse(url).hostname or "", pinned.ip)
            if pinned is not None and pinned.ip is not None
            else None
        )
        try:
            async with (
                httpx.AsyncClient(
                    timeout=per_phase,
                    follow_redirects=False,
                    transport=transport,
                ) as client,
                client.stream(
                    method,
                    url,
                    params=params or None,
                    json=body,
                    headers=headers or None,
                ) as response,
            ):
                status = response.status_code
                resp_headers = httpx.Headers(dict(response.headers))
                is_redirect = (
                    status in _REDIRECT_STATUSES and resp_headers.get("location") is not None
                )
                raw = b"" if is_redirect else (await _read_capped(response, MAX_RESPONSE_BYTES))[0]
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(redact(str(exc))) from exc
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError(redact(str(exc))) from exc
        if status not in _REDIRECT_STATUSES:
            return rebuild_response(status, resp_headers, raw)
        location = resp_headers.get("location")
        if not location:
            return rebuild_response(status, resp_headers, raw)
        hop += 1
        if hop > MAX_REDIRECT_HOPS:
            raise UpstreamRemoteError(f"redirect limit of {MAX_REDIRECT_HOPS} exceeded")
        next_url = urljoin(url, location)
        try:
            next_pinned = await admit_redirect(next_url, policy=policy, hop=hop)
        except ValueError as exc:
            raise UpstreamUnavailableError(
                f"URL policy rejected redirect: {redact(str(exc))}"
            ) from exc
        # Resolve-once: pin the address this hop validated, never re-resolve.
        pinned = next_pinned if next_pinned.ip is not None else None
        same_origin = _origin(next_url) == original_origin
        method, keep_body = _redirect_method(method, status)
        if not keep_body:
            body = None
        if not same_origin:
            # A credential for origin A must never reach origin B, and a
            # mutating method/body must never be replayed cross-origin (R05).
            headers = {
                name: value
                for name, value in headers.items()
                if name not in request.credential_header_names
            }
            params = {
                name: value
                for name, value in params.items()
                if name not in request.credential_param_names
            }
            if method not in _SAFE_METHODS:
                method = "GET"
                body = None
        url = next_url


def rebuild_response(status: int, headers: httpx.Headers, content: bytes) -> httpx.Response:
    """Rebuild a response from already-decoded bytes.

    ``client.stream`` yields decompressed bytes, so the original
    ``Content-Encoding``/``Content-Length``/``Transfer-Encoding`` headers no
    longer describe ``content``. Keeping them makes httpx decode a second time
    and raise ``DecodingError`` (N1), so drop them.
    """
    stripped = httpx.Headers(
        [
            (name, value)
            for name, value in headers.items()
            if name.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
        ]
    )
    return httpx.Response(status, headers=stripped, content=content)


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
    timeout_s = max(timeout_ms, 1) / 1000.0
    # One end-to-end deadline spanning DNS → connect → redirects → body read,
    # rather than httpx's per-phase timeout (R36).
    try:
        async with asyncio.timeout(timeout_s):
            response = await _send(request, timeout_s=timeout_s, policy=policy)
    except TimeoutError as exc:
        raise UpstreamTimeoutError("overall request deadline exceeded") from exc
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
        async with asyncio.timeout(timeout_s):
            async with (
                httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout_s),
                    follow_redirects=False,
                    transport=transport,
                ) as client,
                client.stream("GET", url) as response,
            ):
                status = response.status_code
                resp_headers = httpx.Headers(dict(response.headers))
                raw, truncated = await _read_capped(response, MAX_FETCH_BYTES)
    except TimeoutError as exc:
        raise UpstreamTimeoutError("overall request deadline exceeded") from exc
    except httpx.TimeoutException as exc:
        raise UpstreamTimeoutError(redact(str(exc))) from exc
    except httpx.HTTPError as exc:
        raise UpstreamUnavailableError(redact(str(exc))) from exc
    if truncated:
        raise UpstreamRemoteError(
            f"resource exceeded the {MAX_FETCH_BYTES // (1024 * 1024)} MiB fetch limit"
        )
    response = rebuild_response(status, resp_headers, raw)
    if response.status_code in _REDIRECT_STATUSES:
        raise UpstreamRemoteError("unexpected redirect while fetching resource")
    _raise_for_status(response)
    return response.text
