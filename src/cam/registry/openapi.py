"""OpenAPI import: fetch a spec, validate it, turn operations into REST tools.

Each operation becomes one tool: `operationId` (or a synthesized name) is the
tool name, path/query/header parameters plus the JSON request body flatten
into one input schema, and the binding records where each argument goes. For
large specs the caller passes `select` to import only chosen operations.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from openapi_spec_validator import validate as validate_openapi
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.db.models import Service, Tool
from cam.upstream.rest import fetch_text
from cam.upstream.url import UrlPolicy

_JSON = "application/json"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


class OpenAPIError(Exception):
    """The spec cannot be fetched, validated or converted into tools."""


class _SkipOperation(Exception):
    """One operation cannot be represented as a CAM REST tool."""


@dataclass(frozen=True, slots=True)
class SpecOperation:
    name: str
    method: str
    path: str
    description: str
    input_schema: dict[str, Any]
    binding: dict[str, Any] = field(default_factory=dict[str, Any])


def _as_dict(node: Any) -> dict[str, Any] | None:
    if isinstance(node, dict):
        return cast("dict[str, Any]", node)
    return None


def _as_list(node: Any) -> list[Any] | None:
    if isinstance(node, list):
        return cast("list[Any]", node)
    return None


def _as_mapping(node: Any) -> Mapping[str, Any] | None:
    if isinstance(node, Mapping):
        return cast("Mapping[str, Any]", node)
    return None


def _resolve_refs(node: Any, spec: Mapping[str, Any], seen: frozenset[str]) -> Any:
    items = _as_list(node)
    if items is not None:
        return [_resolve_refs(item, spec, seen) for item in items]
    mapping = _as_mapping(node)
    if mapping is None:
        return node
    ref = mapping.get("$ref")
    if isinstance(ref, str):
        if ref in seen or not ref.startswith("#/"):
            return {"$ref": ref}
        target: Any = spec
        for part in ref[2:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            target_dict = _as_mapping(target)
            if target_dict is None or key not in target_dict:
                raise OpenAPIError(f"unresolvable reference {ref!r}")
            target = target_dict[key]
        return _resolve_refs(target, spec, seen | {ref})
    return {str(key): _resolve_refs(value, spec, seen) for key, value in mapping.items()}


def _deref(spec: Mapping[str, Any]) -> dict[str, Any]:
    resolved = _as_dict(_resolve_refs(spec, spec, frozenset()))
    if resolved is None:
        raise OpenAPIError("spec did not resolve to an object")
    return resolved


def _tool_name(raw: Any, *, method: str, path: str, taken: set[str]) -> str:
    candidate = str(raw).strip() if raw is not None else ""
    if not candidate or not _NAME_RE.match(candidate):
        candidate = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{method}_{path}")
        candidate = re.sub(r"_+", "_", candidate).strip("._-")
    if not candidate:
        candidate = f"{method}_op"
    name = candidate
    counter = 2
    while name in taken:
        name = f"{candidate}_{counter}"
        counter += 1
    return name


def _media_for_json(content: Any) -> Mapping[str, Any] | None:
    mapping = _as_mapping(content)
    if mapping is None or not mapping:
        return None
    direct = _as_mapping(mapping.get(_JSON))
    if direct is not None:
        return direct
    for _ctype, media in mapping.items():
        media_mapping = _as_mapping(media)
        if media_mapping is not None:
            return media_mapping
    return None


def _merge_object_schema(
    body_schema: Mapping[str, Any],
    properties: dict[str, Any],
    required: set[str],
    locations: dict[str, str],
) -> None:
    body_properties = _as_mapping(body_schema.get("properties"))
    if body_properties is not None:
        for key, value in body_properties.items():
            name = str(key)
            if name in locations:
                continue
            value_mapping = _as_mapping(value)
            properties[name] = dict(value_mapping) if value_mapping else {"type": "string"}
            locations[name] = "body"
    body_required = _as_list(body_schema.get("required"))
    if body_required is not None:
        for name in body_required:
            required.add(str(name))


def _schema_or_string(raw: Any) -> dict[str, Any]:
    mapping = _as_mapping(raw)
    if mapping is None:
        return {"type": "string"}
    schema = dict(mapping)
    schema.setdefault("type", "string")
    return schema


def _operation_schema(
    parameters: Sequence[Mapping[str, Any]],
    operation: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    properties: dict[str, Any] = {}
    required: set[str] = set()
    locations: dict[str, str] = {}

    for param in parameters:
        name = str(param.get("name") or "")
        location = str(param.get("in") or "")
        if not name:
            continue
        if location in ("path", "query", "header"):
            schema = _schema_or_string(param.get("schema"))
            description = param.get("description")
            if isinstance(description, str) and description:
                schema.setdefault("description", description)
            properties[name] = schema
            locations[name] = location
            if param.get("required") or location == "path":
                required.add(name)
        elif location == "body":
            body_schema = _as_mapping(param.get("schema"))
            if body_schema is None:
                continue
            body_type = body_schema.get("type")
            if body_type not in (None, "object") and "properties" not in body_schema:
                raise _SkipOperation(f"body parameter {name!r} is not an object schema")
            _merge_object_schema(body_schema, properties, required, locations)
        elif location == "formData":
            schema = _schema_or_string(param.get("schema") or {"type": param.get("type")})
            properties[name] = schema
            locations[name] = "body"
            if param.get("required"):
                required.add(name)

    request_body = _as_mapping(operation.get("requestBody"))
    if request_body is not None:
        media = _media_for_json(request_body.get("content"))
        if media is not None:
            body_schema = _as_mapping(media.get("schema"))
            if body_schema is not None:
                body_type = body_schema.get("type")
                if body_type not in (None, "object") and "properties" not in body_schema:
                    raise _SkipOperation("request body is not an object schema")
                _merge_object_schema(body_schema, properties, required, locations)
        if request_body.get("required") and not properties:
            raise _SkipOperation("required request body has no object properties")

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = sorted(required)
    return schema, locations


def _as_param_list(raw: Any) -> list[Mapping[str, Any]]:
    items = _as_list(raw)
    if items is None:
        return []
    params: list[Mapping[str, Any]] = []
    for item in items:
        mapping = _as_mapping(item)
        if mapping is not None:
            params.append(mapping)
    return params


def parse_operations(spec: Mapping[str, Any]) -> list[SpecOperation]:
    resolved = _deref(spec)
    paths = _as_mapping(resolved.get("paths"))
    if paths is None:
        raise OpenAPIError("spec has no paths object")
    operations: list[SpecOperation] = []
    taken: set[str] = set()
    for path, path_item_raw in paths.items():
        path_item = _as_mapping(path_item_raw)
        if path_item is None:
            continue
        shared_params = _as_param_list(path_item.get("parameters"))
        for method in _HTTP_METHODS:
            operation = _as_mapping(path_item.get(method))
            if operation is None:
                continue
            merged: dict[tuple[str, str], Mapping[str, Any]] = {
                (str(p.get("name") or ""), str(p.get("in") or "")): p for p in shared_params
            }
            for param in _as_param_list(operation.get("parameters")):
                merged[(str(param.get("name") or ""), str(param.get("in") or ""))] = param
            try:
                input_schema, locations = _operation_schema(list(merged.values()), operation)
            except _SkipOperation:
                continue
            name = _tool_name(
                operation.get("operationId"), method=method, path=str(path), taken=taken
            )
            taken.add(name)
            summary = operation.get("summary")
            description = operation.get("description")
            text = (
                str(summary).strip()
                if isinstance(summary, str) and summary.strip()
                else (
                    str(description).strip()
                    if isinstance(description, str) and description.strip()
                    else f"{method.upper()} {path}"
                )
            )
            operations.append(
                SpecOperation(
                    name=name,
                    method=method.upper(),
                    path=str(path),
                    description=text,
                    input_schema=input_schema,
                    binding={"method": method.upper(), "path": str(path), "params": locations},
                )
            )
    if not operations:
        raise OpenAPIError("spec contains no importable operations")
    return operations


def _parse_json_spec(text: str) -> dict[str, Any]:
    try:
        loaded: Any = json.loads(text)
    except ValueError as exc:
        raise OpenAPIError("spec URL did not return JSON") from exc
    parsed = _as_dict(loaded)
    if parsed is None:
        raise OpenAPIError("spec URL did not return a JSON object")
    return parsed


def _validate(spec: dict[str, Any]) -> None:
    try:
        validate_openapi(spec)
    except Exception as exc:
        raise OpenAPIError(f"invalid OpenAPI spec: {exc}") from exc


async def fetch_spec(
    url: str,
    *,
    url_policy: UrlPolicy | None = None,
    timeout_s: float = 15.0,
) -> dict[str, Any]:
    try:
        text = await fetch_text(url, timeout_s=timeout_s, url_policy=url_policy)
    except Exception as exc:
        raise OpenAPIError(f"could not fetch spec: {exc}") from exc
    spec = _parse_json_spec(text)
    _validate(spec)
    return spec


async def preview_openapi(
    url: str,
    *,
    url_policy: UrlPolicy | None = None,
) -> list[SpecOperation]:
    spec = await fetch_spec(url, url_policy=url_policy)
    return parse_operations(spec)


async def import_openapi_tools(
    session: AsyncSession,
    *,
    service: Service,
    spec: Mapping[str, Any] | None = None,
    spec_url: str | None = None,
    select_names: Sequence[str] | None = None,
    url_policy: UrlPolicy | None = None,
) -> list[Tool]:
    if spec is None:
        if not spec_url:
            raise OpenAPIError("import needs a spec or a spec_url")
        spec = await fetch_spec(spec_url, url_policy=url_policy)
    operations = parse_operations(spec)
    if select_names is not None:
        wanted = set(select_names)
        known = {operation.name for operation in operations}
        unknown = wanted - known
        if unknown:
            raise OpenAPIError(f"unknown tool(s): {', '.join(sorted(unknown))}")
        operations = [operation for operation in operations if operation.name in wanted]
    existing_rows = await session.execute(select(Tool).where(Tool.service_id == service.id))
    existing = {tool.name: tool for tool in existing_rows.scalars()}
    tools: list[Tool] = []
    for operation in operations:
        row = existing.get(operation.name)
        if row is None:
            row = Tool(
                org_id=service.org_id,
                service_id=service.id,
                name=operation.name,
                upstream_name=operation.name,
                description=operation.description,
                input_schema=operation.input_schema,
                rest_binding=operation.binding,
                enabled=True,
                status="active",
            )
            session.add(row)
        else:
            row.upstream_name = operation.name
            row.description = operation.description
            row.input_schema = operation.input_schema
            row.rest_binding = operation.binding
        tools.append(row)
    await session.flush()
    return tools
