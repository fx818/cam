"""Manual REST tool definition: validate a binding against the service URL,
then upsert one Tool row. The dry run goes through the same builder the
executor uses, so a definition that saves is a definition that can fire.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.db.models import Service, Tool
from cam.registry.examples import sample_invocation
from cam.registry.services import ServiceError
from cam.upstream.rest import HTTP_METHODS, PARAM_LOCATIONS, RestBindingError, build_rest_request

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


class RestToolError(ServiceError):
    """A manual REST tool definition is invalid."""


def _validate_name(name: str) -> str:
    resolved = name.strip()
    if not _NAME_RE.match(resolved):
        raise RestToolError(
            "tool name must start with a letter or digit and contain only "
            "letters, digits, dots, underscores or hyphens"
        )
    return resolved


def _validate_method(method: str) -> str:
    resolved = method.strip().upper()
    if resolved not in HTTP_METHODS:
        raise RestToolError(
            f"method must be one of {', '.join(sorted(HTTP_METHODS))}; got {method!r}"
        )
    return resolved


def _validate_path_and_params(path: str, params: Mapping[str, str]) -> None:
    resolved = path.strip()
    if not resolved.startswith("/"):
        raise RestToolError("path must start with /")
    if "://" in resolved:
        raise RestToolError("path must be relative to the service URL")
    placeholders = _PLACEHOLDER_RE.findall(resolved)
    for placeholder in placeholders:
        if params.get(placeholder) != "path":
            raise RestToolError(
                f"path placeholder {{{placeholder}}} needs a parameter with location 'path'"
            )
    for name, location in params.items():
        if location not in PARAM_LOCATIONS:
            raise RestToolError(
                f"parameter {name!r} has unknown location {location!r}; "
                f"expected one of {sorted(PARAM_LOCATIONS)}"
            )
        if location == "path" and name not in placeholders:
            raise RestToolError(
                f"parameter {name!r} is marked path but does not appear in the path"
            )


def _derive_input_schema(params: Mapping[str, str]) -> dict[str, Any]:
    properties: dict[str, Any] = {name: {"type": "string"} for name in params}
    required = sorted(name for name, location in params.items() if location == "path")
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _resolve_input_schema(
    input_schema: Mapping[str, Any] | None, params: Mapping[str, str]
) -> dict[str, Any]:
    if input_schema is None:
        return _derive_input_schema(params)
    if input_schema.get("type") != "object":
        raise RestToolError("input_schema must be an object schema")
    properties = input_schema.get("properties")
    if not isinstance(properties, Mapping):
        raise RestToolError("input_schema must have properties")
    for name, location in params.items():
        if location == "path" and name not in properties:
            raise RestToolError(f"input_schema is missing the path parameter {name!r}")
    return dict(input_schema)


def _dry_run(base_url: str, binding: Mapping[str, Any], params: Mapping[str, str]) -> None:
    placeholder_args = {name: "placeholder" for name in params}
    try:
        build_rest_request(base_url, binding, placeholder_args)
    except RestBindingError as exc:
        raise RestToolError(f"invalid binding: {exc}") from exc


async def define_rest_tool(
    session: AsyncSession,
    *,
    service: Service,
    name: str,
    method: str,
    path: str,
    params: Mapping[str, str] | None = None,
    description: str = "",
    input_schema: Mapping[str, Any] | None = None,
) -> Tool:
    if service.kind != "rest":
        raise RestToolError("manual REST tools require a service with kind 'rest'")
    resolved_name = _validate_name(name)
    resolved_method = _validate_method(method)
    resolved_path = path.strip()
    resolved_params = {str(key): str(value) for key, value in (params or {}).items()}
    _validate_path_and_params(resolved_path, resolved_params)

    binding: dict[str, Any] = {
        "method": resolved_method,
        "path": resolved_path,
        "params": resolved_params,
    }
    _dry_run(service.url, binding, resolved_params)
    resolved_schema = _resolve_input_schema(input_schema, resolved_params)
    resolved_description = description.strip() or f"{resolved_method} {resolved_path}"

    existing = await session.execute(
        select(Tool).where(Tool.service_id == service.id, Tool.name == resolved_name)
    )
    row = existing.scalar_one_or_none()
    if row is None:
        row = Tool(
            org_id=service.org_id,
            service_id=service.id,
            name=resolved_name,
            upstream_name=resolved_name,
            description=resolved_description,
            input_schema=resolved_schema,
            rest_binding=binding,
            example_invocation=sample_invocation(resolved_schema),
            enabled=True,
            status="active",
        )
        session.add(row)
    else:
        row.description = resolved_description
        row.input_schema = resolved_schema
        if row.example_invocation is None:
            row.example_invocation = sample_invocation(resolved_schema)
        row.rest_binding = binding
        row.status = "active"
    await session.flush()
    return row
