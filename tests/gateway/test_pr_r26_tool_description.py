"""R26: the per-user `cam` tool description includes connected templates."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cam.access import ServiceSummary
from cam.gateway import server as server_module
from cam.gateway.server import SkillDescription, ToolDescriptionMiddleware, build_tool_description


def _template_summary(ref: str) -> ServiceSummary:
    return ServiceSummary(
        service_id=str(uuid.uuid4()),
        id=ref,
        name="Notion",
        description="Notion template",
        tier="template",
        kind="mcp",
        owner=None,
        shared_by=None,
        health="healthy",
        tool_count=3,
        connected=True,
    )


def test_build_tool_description_lists_template_services_and_skills() -> None:
    text = build_tool_description(
        [_template_summary("global/notion")],
        [SkillDescription("me/guide", "A guide", True)],
    )
    assert "global/notion" in text
    assert "skill me/guide" in text


class _FakeSession:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _fake_session_factory() -> _FakeSession:
    return _FakeSession()


async def test_description_middleware_includes_templates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_list_services(
        session: object, identity: object, *, include_templates: bool = False
    ) -> list[ServiceSummary]:
        captured["include_templates"] = include_templates
        return [_template_summary("global/notion")]

    async def fake_list_skills(session: object, org_id: object, user: object) -> list[Any]:
        return []

    monkeypatch.setattr(server_module, "list_services", fake_list_services)
    monkeypatch.setattr(server_module, "access_list_skills", fake_list_skills)

    def fake_identity(request: object) -> object:
        return SimpleNamespace(org_id=uuid.uuid4(), user=SimpleNamespace())

    monkeypatch.setattr(server_module, "_request_identity", fake_identity)

    middleware = ToolDescriptionMiddleware(cast("Any", _fake_session_factory))
    ctx = cast("Any", SimpleNamespace(method="tools/list", request=object()))

    async def call_next(_ctx: object) -> dict[str, Any]:
        return {"tools": [{"name": "cam", "description": "original"}]}

    result = cast("Any", await middleware(ctx, cast("Any", call_next)))
    assert captured["include_templates"] is True
    tools = cast("list[dict[str, Any]]", result["tools"])
    assert "global/notion" in tools[0]["description"]


async def test_description_middleware_skips_non_tools_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"list_services": False}

    async def fake_list_services(
        session: object, identity: object, *, include_templates: bool = False
    ) -> list[ServiceSummary]:
        called["list_services"] = True
        return []

    monkeypatch.setattr(server_module, "list_services", fake_list_services)
    middleware = ToolDescriptionMiddleware(cast("Any", _fake_session_factory))
    ctx = cast("Any", SimpleNamespace(method="tools/call", request=object()))

    async def call_next(_ctx: object) -> dict[str, Any]:
        return {"tools": []}

    await middleware(ctx, cast("Any", call_next))
    assert called["list_services"] is False
