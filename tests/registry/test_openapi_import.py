"""OpenAPI import: parse, validate, select and persist REST tools."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.connections.orgs import ensure_org
from cam.db.models import Service, Tool
from cam.registry.openapi import (
    OpenAPIError,
    SpecOperation,
    fetch_spec,
    import_openapi_tools,
    parse_operations,
    preview_openapi,
)
from fixtures.mock_rest import OPENAPI_SPEC, MockRestHandle

_MINIMAL_SPEC: dict[str, object] = {
    "openapi": "3.0.4",
    "info": {"title": "Tiny", "version": "1.0.0"},
    "paths": {
        "/widgets/{wid}": {
            "get": {
                "summary": "Get widget",
                "parameters": [
                    {"name": "wid", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "verbose", "in": "query", "schema": {"type": "boolean"}},
                ],
                "responses": {"200": {"description": "ok"}},
            },
            "delete": {
                "responses": {"204": {"description": "gone"}},
            },
        },
        "/widgets": {
            "post": {
                "operationId": "createWidget",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "tags": {"type": "array", "items": {"type": "string"}},
                                },
                            }
                        }
                    },
                },
                "responses": {"201": {"description": "created"}},
            },
        },
    },
}


def test_parse_mock_spec_operations() -> None:
    operations = parse_operations(OPENAPI_SPEC)
    names = {operation.name for operation in operations}
    assert {
        "listItems",
        "createItem",
        "getItem",
        "whoami",
        "plainText",
        "missing",
        "boom",
        "throttled",
        "slow",
        "redirect",
        "openEndpoint",
    } <= names

    by_name = {operation.name: operation for operation in operations}
    get_item = by_name["getItem"]
    assert get_item.method == "GET"
    assert get_item.path == "/items/{item_id}"
    assert get_item.binding["params"] == {"item_id": "path"}
    assert get_item.input_schema["required"] == ["item_id"]
    assert "item_id" in get_item.input_schema["properties"]

    list_items = by_name["listItems"]
    assert list_items.binding["params"] == {"q": "query", "limit": "query"}
    assert set(list_items.input_schema["properties"]) == {"q", "limit"}
    assert "required" not in list_items.input_schema

    create = by_name["createItem"]
    assert create.method == "POST"
    assert create.binding["params"] == {"name": "body", "tags": "body"}
    assert create.input_schema["required"] == ["name"]
    assert set(create.input_schema["properties"]) == {"name", "tags"}


def test_parse_synthesizes_names_and_defaults() -> None:
    operations = parse_operations(_MINIMAL_SPEC)
    names = {operation.name for operation in operations}
    assert names == {"get_widgets_wid", "delete_widgets_wid", "createWidget"}

    by_name = {operation.name: operation for operation in operations}
    get = by_name["get_widgets_wid"]
    assert get.binding["params"] == {"wid": "path", "verbose": "query"}
    assert get.input_schema["required"] == ["wid"]
    assert get.description == "Get widget"

    delete = by_name["delete_widgets_wid"]
    assert delete.method == "DELETE"
    assert delete.binding["params"] == {}
    assert delete.input_schema == {"type": "object", "properties": {}}


def test_parse_resolves_local_refs() -> None:
    spec = {
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
    assert operations[0].input_schema["required"] == ["tenant"]


def test_parse_skips_non_object_bodies() -> None:
    spec = {
        "openapi": "3.0.4",
        "info": {"title": "Arr", "version": "1"},
        "paths": {
            "/list": {
                "post": {
                    "operationId": "postList",
                    "requestBody": {"content": {"application/json": {"schema": {"type": "array"}}}},
                    "responses": {"200": {"description": "ok"}},
                },
                "get": {"responses": {"200": {"description": "ok"}}},
            }
        },
    }
    operations = parse_operations(spec)
    assert [operation.name for operation in operations] == ["get_list"]


def test_parse_rejects_empty_and_broken_specs() -> None:
    with pytest.raises(OpenAPIError, match="no paths"):
        parse_operations({"openapi": "3.0.4", "info": {"title": "x", "version": "1"}})
    with pytest.raises(OpenAPIError, match="no importable"):
        parse_operations({"openapi": "3.0.4", "info": {"title": "x", "version": "1"}, "paths": {}})
    with pytest.raises(OpenAPIError, match="unresolvable"):
        parse_operations(
            {
                "openapi": "3.0.4",
                "info": {"title": "x", "version": "1"},
                "paths": {
                    "/a": {
                        "get": {
                            "parameters": [{"$ref": "#/components/parameters/Missing"}],
                            "responses": {"200": {"description": "ok"}},
                        }
                    }
                },
            }
        )


async def test_fetch_spec_from_mock(mock_rest: MockRestHandle) -> None:
    spec = await fetch_spec(mock_rest.spec_url)
    assert spec["info"]["title"] == "CAM Mock REST API"


async def test_fetch_spec_invalid_document(mock_rest: MockRestHandle) -> None:
    with pytest.raises(OpenAPIError, match="invalid OpenAPI spec"):
        await fetch_spec(f"{mock_rest.base_url}/open")


async def test_preview_lists_operations(mock_rest: MockRestHandle) -> None:
    operations = await preview_openapi(mock_rest.spec_url)
    assert isinstance(operations[0], SpecOperation)
    assert {operation.name for operation in operations} >= {"listItems", "getItem"}


async def _rest_service(session: AsyncSession, slug: str) -> Service:
    org = await ensure_org(session)
    service = Service(
        org_id=org.id,
        tier="global",
        owner_user_id=None,
        slug=slug,
        name="Import Target",
        kind="rest",
        url="http://127.0.0.1:9",
        transport=None,
        auth_type="none",
        auth_config={},
        visibility="all",
        status="active",
        health="unknown",
    )
    session.add(service)
    await session.flush()
    return service


async def test_import_creates_tool_rows(db_session: AsyncSession) -> None:
    service = await _rest_service(db_session, f"imp-{id(object()):x}")
    tools = await import_openapi_tools(
        db_session, service=service, spec=json.loads(json.dumps(OPENAPI_SPEC))
    )
    await db_session.commit()
    names = {tool.name for tool in tools}
    assert {"listItems", "getItem", "createItem"} <= names
    stored = (
        await db_session.execute(
            select(Tool).where(Tool.name == "getItem", Tool.service_id == service.id)
        )
    ).scalar_one()
    assert stored.rest_binding == {
        "method": "GET",
        "path": "/items/{item_id}",
        "params": {"item_id": "path"},
    }
    assert stored.enabled is True


async def test_import_selection_and_unknown(db_session: AsyncSession) -> None:
    service = await _rest_service(db_session, f"sel-{id(object()):x}")
    tools = await import_openapi_tools(
        db_session,
        service=service,
        spec=OPENAPI_SPEC,
        select_names=["listItems"],
    )
    await db_session.commit()
    assert [tool.name for tool in tools] == ["listItems"]

    other = await _rest_service(db_session, f"sel2-{id(object()):x}")
    with pytest.raises(OpenAPIError, match="unknown tool"):
        await import_openapi_tools(
            db_session, service=other, spec=OPENAPI_SPEC, select_names=["nope"]
        )


async def test_reimport_updates_existing_rows(db_session: AsyncSession) -> None:
    service = await _rest_service(db_session, f"re-{id(object()):x}")
    first = await import_openapi_tools(db_session, service=service, spec=OPENAPI_SPEC)
    await db_session.commit()
    count_first = len(first)

    again = await import_openapi_tools(
        db_session, service=service, spec=OPENAPI_SPEC, select_names=["listItems"]
    )
    await db_session.commit()
    assert len(again) == 1
    total = (
        (await db_session.execute(select(Tool).where(Tool.service_id == service.id)))
        .scalars()
        .all()
    )
    assert len(total) == count_first
