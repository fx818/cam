"""N1 regression: compressed REST responses must not be double-decoded.

``client.stream`` yields decompressed bytes, so the rebuilt response must drop
the original Content-Encoding/Content-Length headers. Keeping them made httpx
decode a second time and raise ``DecodingError`` after the upstream call had
already run.
"""

from __future__ import annotations

import gzip

import httpx

from cam.upstream.rest import rebuild_response


def test_rebuild_response_drops_encoding_headers() -> None:
    original = httpx.Headers(
        {
            "content-type": "application/json",
            "content-encoding": "gzip",
            "content-length": "999",
            "transfer-encoding": "chunked",
        }
    )
    # ``payload`` is already the decoded body, exactly what client.stream yields.
    payload = b'{"ok": true}'
    rebuilt = rebuild_response(200, original, payload)

    assert rebuilt.status_code == 200
    assert rebuilt.headers.get("content-encoding") is None
    assert rebuilt.headers.get("transfer-encoding") is None
    assert rebuilt.headers.get("content-type") == "application/json"
    # Reading the body must not attempt a second decode.
    assert rebuilt.text == '{"ok": true}'


def test_gzip_bytes_survive_single_decode() -> None:
    body = b'{"value": 42}'
    gz = gzip.compress(body)
    response = httpx.Response(
        200,
        headers=httpx.Headers({"content-encoding": "gzip"}),
        content=gz,
    )
    # A response built from the *encoded* bytes with the header decodes once.
    assert response.json() == {"value": 42}
