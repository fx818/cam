"""R04: credentials never reach logs (redaction + transport-logger suppression)."""

from __future__ import annotations

import logging

from cam.api.main import CredentialRedactionFilter, configure_logging, redact_credentials
from cam.config import Settings


def test_redact_credentials_query_params() -> None:
    text = 'HTTP Request: GET https://ex.test/mcp?token=SEKRET&page=2 "200 OK"'
    redacted = redact_credentials(text)
    assert "SEKRET" not in redacted
    assert "token=***" in redacted
    assert "page=2" in redacted


def test_redact_credentials_authorization_header() -> None:
    assert redact_credentials("Authorization: Bearer abc.def.ghi") == "Authorization: Bearer ***"
    assert redact_credentials("Basic dXNlcjpwYXNz") == "Basic ***"


def test_redaction_filter_scrubs_record() -> None:
    record = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1, "GET /mcp?api_key=SECRET ok", None, None
    )
    assert CredentialRedactionFilter().filter(record) is True
    assert "SECRET" not in record.getMessage()


def test_configure_logging_suppresses_transport_loggers() -> None:
    configure_logging(Settings(log_level="INFO"))
    for name in ("httpx", "httpx2"):
        assert logging.getLogger(name).level >= logging.WARNING
