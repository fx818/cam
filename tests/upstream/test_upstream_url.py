"""URL admission choke-point tests."""

import pytest

from cam.upstream.url import admit_url


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
