"""URL admission for every outbound upstream connection.

Phase 1 only enforces scheme/host shape. Phase 2.5's SSRF policy hooks in
here (resolve, validate IPs, pin the connection) without touching call sites.
"""

from __future__ import annotations

from urllib.parse import urlparse

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def admit_url(url: str) -> str:
    """Validate `url` for outbound use and return it unchanged.

    Raises `ValueError` when the URL is not an absolute http(s) URL with a host.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"unsupported URL scheme {parsed.scheme!r}; expected http or https")
    if not parsed.hostname:
        raise ValueError("URL must include a host")
    if parsed.username or parsed.password:
        raise ValueError("URL must not embed credentials")
    return url
