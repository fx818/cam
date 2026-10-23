"""R01: stored schemas must never trigger an external ``$ref`` fetch.

jsonschema's default registry resolves an unknown ``$ref`` with a synchronous
``urllib`` fetch. These tests prove CAM's validation path, OpenAPI import and
manual REST tool definitions all refuse non-local references without touching
the network or the filesystem, while local ``#/`` references keep working.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cam.db.models import Service
from cam.invoke.errors import CamError, CamErrorCode
from cam.invoke.validation import validate_args
from cam.registry.openapi import OpenAPIError, parse_operations
from cam.registry.rest_tools import RestToolError, _resolve_input_schema, define_rest_tool

_LOCAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"a": {"$ref": "#/$defs/S"}},
    "$defs": {"S": {"type": "string"}},
    "required": ["a"],
}


# --- validate_args -----------------------------------------------------------


def test_validate_args_resolves_local_refs() -> None:
    validate_args(_LOCAL_SCHEMA, {"a": "x"})
    with pytest.raises(CamError) as exc_info:
        validate_args(_LOCAL_SCHEMA, {"a": 1})
    assert exc_info.value.code is CamErrorCode.INVALID_ARGS


def test_validate_args_skips_http_ref_without_fetching(monkeypatch: pytest.MonkeyPatch) -> None:
    fetches: list[str] = []

    def _fail(uri: Any, *args: Any, **kwargs: Any) -> Any:
        fetches.append(str(uri))
        raise AssertionError(f"jsonschema attempted a remote fetch of {uri!r}")

    monkeypatch.setattr("urllib.request.urlopen", _fail)
    schema = {"type": "object", "properties": {"a": {"$ref": "http://127.0.0.1:9/s.json#/S"}}}

    # A policy-rejecting schema must not fail the call: log + skip, no raise.
    validate_args(schema, {"a": 1})
    assert fetches == []


def test_validate_args_skips_file_ref_without_reading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetches: list[str] = []

    def _fail(uri: Any, *args: Any, **kwargs: Any) -> Any:
        fetches.append(str(uri))
        raise AssertionError(f"jsonschema attempted to read {uri!r}")

    monkeypatch.setattr("urllib.request.urlopen", _fail)
    target = tmp_path / "evil.json"
    # If the file were read, this fragment would reject {"a": 1}; not reading it
    # means validation is skipped instead.
    target.write_text(json.dumps({"S": {"type": "string"}}), encoding="utf-8")
    schema = {
        "type": "object",
        "properties": {"a": {"$ref": f"file://{target}#/S"}},
        "required": ["a"],
    }

    validate_args(schema, {"a": 1})
    assert fetches == []


def test_validate_args_still_accepts_meta_schema() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"a": {"type": "string"}},
    }
    validate_args(schema, {"a": "x"})


# --- OpenAPI import ----------------------------------------------------------


def _spec_with_ref(ref: str) -> dict[str, Any]:
    return {
        "openapi": "3.0.4",
        "info": {"title": "x", "version": "1"},
        "paths": {
            "/a": {
                "get": {
                    "parameters": [{"$ref": ref}],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }


def test_parse_operations_rejects_http_ref() -> None:
    with pytest.raises(OpenAPIError, match="non-local"):
        parse_operations(_spec_with_ref("http://127.0.0.1:9/x.json#/p"))


def test_parse_operations_rejects_file_ref() -> None:
    with pytest.raises(OpenAPIError, match="non-local"):
        parse_operations(_spec_with_ref("file:///etc/passwd#/p"))


def test_parse_operations_still_resolves_local_refs() -> None:
    spec: dict[str, Any] = {
        "openapi": "3.0.4",
        "info": {"title": "Refs", "version": "1"},
        "paths": {
            "/a": {
                "get": {
                    "parameters": [{"$ref": "#/components/parameters/Tenant"}],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
        "components": {
            "parameters": {
                "Tenant": {
                    "name": "tenant",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string"},
                }
            }
        },
    }
    operations = parse_operations(spec)
    assert operations[0].binding["params"] == {"tenant": "query"}


# --- manual REST tools -------------------------------------------------------


def test_resolve_input_schema_rejects_external_ref() -> None:
    with pytest.raises(RestToolError, match="reference outside"):
        _resolve_input_schema(
            {"type": "object", "properties": {"a": {"$ref": "http://evil.example/x.json#/a"}}},
            {},
        )


def test_resolve_input_schema_allows_local_ref() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/S"}},
        "$defs": {"S": {"type": "string"}},
    }
    assert _resolve_input_schema(schema, {}) == schema


async def test_define_rest_tool_rejects_external_ref(db_session: AsyncSession) -> None:
    service = Service(
        org_id=uuid.uuid4(),
        tier="global",
        slug="pr-r01",
        name="X",
        kind="rest",
        url="https://example.com/api",
    )
    with pytest.raises(RestToolError, match="reference outside"):
        await define_rest_tool(
            db_session,
            service=service,
            name="t",
            method="GET",
            path="/a",
            params={},
            input_schema={
                "type": "object",
                "properties": {"a": {"$ref": "file:///etc/hostname#/a"}},
            },
        )
