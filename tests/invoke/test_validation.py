"""Unit tests for invoke argument validation against stored tool schemas."""

from __future__ import annotations

from typing import Any

import pytest

from cam.invoke.errors import CamError, CamErrorCode
from cam.invoke.validation import validate_args

ECHO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}


def test_valid_arguments_pass() -> None:
    validate_args(ECHO_SCHEMA, {"text": "hello"})
    validate_args(None, {"anything": True})
    validate_args({}, {"anything": True})


def test_missing_required_argument_raises_invalid_args() -> None:
    with pytest.raises(CamError) as excinfo:
        validate_args(ECHO_SCHEMA, {})
    error = excinfo.value
    assert error.code is CamErrorCode.INVALID_ARGS
    assert "text" in error.message
    assert "required" in error.message
    assert error.service is None


def test_wrong_type_raises_invalid_args_with_path() -> None:
    with pytest.raises(CamError) as excinfo:
        validate_args(ECHO_SCHEMA, {"text": 123})
    error = excinfo.value
    assert error.code is CamErrorCode.INVALID_ARGS
    assert "text" in error.message
    assert "not of type" in error.message


def test_nested_path_in_message() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "value": {
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": ["n"],
            }
        },
        "required": ["value"],
    }
    with pytest.raises(CamError) as excinfo:
        validate_args(schema, {"value": {}})
    message = excinfo.value.message
    assert excinfo.value.code is CamErrorCode.INVALID_ARGS
    assert "value" in message
    assert "'n' is a required property" in message


def test_additional_properties_rejected_when_schema_forbids() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "additionalProperties": False,
    }
    with pytest.raises(CamError) as excinfo:
        validate_args(schema, {"text": "ok", "junk": 1})
    assert excinfo.value.code is CamErrorCode.INVALID_ARGS
    assert "junk" in excinfo.value.message


def test_broken_stored_schema_is_skipped() -> None:
    validate_args({"type": "not-a-real-type"}, {"text": "x"})
    validate_args({"$ref": "http://127.0.0.1:9/unresolvable.json"}, {"text": "x"})
