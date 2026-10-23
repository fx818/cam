"""R07: text, structured JSON and error envelopes share one response budget."""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cam.audit.writer import AuditWriter
from cam.config import Settings
from cam.connections.orgs import ensure_org
from cam.connections.tokens import AuthenticatedIdentity, create_token
from cam.db.models import Connection, Service, Tool, User
from cam.db.session import create_db_engine, create_session_factory
from cam.invoke import pipeline
from cam.invoke.pipeline import AgentInfo, GatewayDeps, handle
from cam.invoke.rate_limit import RateLimiter
from cam.invoke.truncation import chars_for_tokens, text_chars, truncate_structured
from cam.upstream.pool import SessionPool
from cam.upstream.types import ImageBlock, TextBlock, ToolCallOutcome

# --- unit: truncate_structured / text_chars ----------------------------------


def test_truncate_structured_small_value_untouched() -> None:
    value = {"a": 1}
    assert truncate_structured(value, cap_tokens=50_000) == (value, False)


def test_truncate_structured_none_untouched() -> None:
    assert truncate_structured(None, cap_tokens=10) == (None, False)


def test_truncate_structured_keeps_leading_keys() -> None:
    value = {f"k{i}": "v" for i in range(200)}
    out, cut = truncate_structured(value, cap_tokens=300)
    assert cut is True
    assert isinstance(out, dict)
    kept = cast("dict[str, Any]", out)
    assert 0 < len(kept) < 200
    assert list(kept) == [f"k{i}" for i in range(len(kept))]
    assert len(json.dumps(kept)) <= chars_for_tokens(300)


def test_truncate_structured_keeps_leading_items() -> None:
    value = [{"i": i} for i in range(200)]
    out, cut = truncate_structured(value, cap_tokens=300)
    assert cut is True
    assert isinstance(out, list)
    kept = cast("list[Any]", out)
    assert 0 < len(kept) < 200
    assert kept == value[: len(kept)]
    assert len(json.dumps(kept)) <= chars_for_tokens(300)


def test_truncate_structured_single_oversized_value_returns_marker() -> None:
    out, cut = truncate_structured({"big": "x" * 10_000}, cap_tokens=100)
    assert cut is True
    assert isinstance(out, dict)
    marker = cast("dict[str, Any]", out)
    assert marker.get("_cam_truncated") is True
    assert len(json.dumps(marker)) <= chars_for_tokens(100)


def test_truncate_structured_without_budget_drops_value() -> None:
    assert truncate_structured({"a": "x" * 100}, cap_tokens=100, max_chars=0) == (None, True)


def test_text_chars_counts_only_text_blocks() -> None:
    content = (TextBlock(text="abc"), ImageBlock(data="zzzz"))
    assert text_chars(content) == 3


# --- pipeline: one budget for text + structured ------------------------------


async def _invoke_env(
    db_session: AsyncSession, *, cap_tokens: int
) -> tuple[AuthenticatedIdentity, str]:
    org = await ensure_org(db_session)
    handle_name = f"pr{uuid.uuid4().hex[:8]}"
    user = User(
        org_id=org.id,
        email=f"{handle_name}@example.com",
        handle=handle_name,
        display_name="PR User",
        password_hash="x",
        role="user",
        kind="person",
    )
    db_session.add(user)
    await db_session.flush()
    _raw, token = await create_token(db_session, org_id=org.id, user_id=user.id, label="pr")
    service = Service(
        org_id=org.id,
        tier="personal",
        owner_user_id=user.id,
        slug=f"svc-{uuid.uuid4().hex[:8]}",
        name="PR REST",
        kind="rest",
        url="https://example.com/api",
        transport=None,
        auth_type="none",
        auth_config={},
        visibility="all",
        status="active",
        health="unknown",
        response_cap_tokens=cap_tokens,
    )
    db_session.add(service)
    await db_session.flush()
    connection = Connection(org_id=org.id, service_id=service.id, user_id=user.id, key_version=1)
    db_session.add(connection)
    await db_session.flush()
    tool = Tool(
        org_id=org.id,
        service_id=service.id,
        name="run",
        upstream_name="run",
        description="run it",
        input_schema={"type": "object", "properties": {}},
        enabled=True,
        status="active",
    )
    db_session.add(tool)
    await db_session.flush()
    await db_session.commit()
    identity = AuthenticatedIdentity(org_id=org.id, user=user, token=token)
    return identity, f"{handle_name}/{service.slug}/run"


def _deps(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[GatewayDeps, SessionPool, AuditWriter]:
    pool = SessionPool(idle_timeout_s=60.0, call_timeout_s=10.0)
    audit = AuditWriter(session_factory)
    deps = GatewayDeps(
        session_factory=session_factory,
        pool=pool,
        settings=Settings(),
        rate_limiter=RateLimiter(60),
        audit=audit,
    )
    return deps, pool, audit


async def test_invoke_caps_structured_under_shared_budget(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, tool_id = await _invoke_env(db_session, cap_tokens=300)
    engine = create_db_engine(Settings().database_url)
    factory = create_session_factory(engine)
    deps, pool, audit = _deps(factory)
    structured = {f"k{i}": "v" for i in range(200)}

    async def _fake_execute_rest(**kwargs: Any) -> ToolCallOutcome:
        return ToolCallOutcome(
            content=(TextBlock(text="ok"),), structured=structured, is_error=False
        )

    monkeypatch.setattr(pipeline, "execute_rest", _fake_execute_rest)
    try:
        result = await handle(
            deps,
            identity,
            AgentInfo(name="pr", version="0"),
            action="invoke",
            id=tool_id,
            args={},
        )
    finally:
        await audit.aclose()
        await pool.aclose()
        await engine.dispose()

    assert result.is_error is False
    assert result.truncated is True
    assert isinstance(result.structured, dict)
    assert result.structured != structured
    remaining = chars_for_tokens(300) - len("ok")
    assert len(json.dumps(result.structured)) <= remaining
    assert 0 < len(result.structured) < 200
    assert list(result.structured) == [f"k{i}" for i in range(len(result.structured))]


async def test_invoke_drops_structured_when_text_uses_the_budget(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, tool_id = await _invoke_env(db_session, cap_tokens=50)
    engine = create_db_engine(Settings().database_url)
    factory = create_session_factory(engine)
    deps, pool, audit = _deps(factory)

    async def _fake_execute_rest(**kwargs: Any) -> ToolCallOutcome:
        return ToolCallOutcome(
            content=(TextBlock(text="L" * 1_000),),
            structured={"a": "x" * 100},
            is_error=False,
        )

    monkeypatch.setattr(pipeline, "execute_rest", _fake_execute_rest)
    try:
        result = await handle(
            deps,
            identity,
            AgentInfo(name="pr", version="0"),
            action="invoke",
            id=tool_id,
            args={},
        )
    finally:
        await audit.aclose()
        await pool.aclose()
        await engine.dispose()

    assert result.truncated is True
    assert result.structured is None
    text_block = result.content[0]
    assert isinstance(text_block, TextBlock)
    assert len(text_block.text) <= chars_for_tokens(50)


async def test_invoke_caps_upstream_error_envelope(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, tool_id = await _invoke_env(db_session, cap_tokens=50)
    engine = create_db_engine(Settings().database_url)
    factory = create_session_factory(engine)
    deps, pool, audit = _deps(factory)

    async def _fake_execute_rest(**kwargs: Any) -> ToolCallOutcome:
        return ToolCallOutcome(
            content=(TextBlock(text="E" * 5_000),), structured=None, is_error=True
        )

    monkeypatch.setattr(pipeline, "execute_rest", _fake_execute_rest)
    try:
        result = await handle(
            deps,
            identity,
            AgentInfo(name="pr", version="0"),
            action="invoke",
            id=tool_id,
            args={},
        )
    finally:
        await audit.aclose()
        await pool.aclose()
        await engine.dispose()

    assert result.is_error is True
    block = result.content[0]
    assert isinstance(block, TextBlock)
    # The envelope stays valid JSON and the unbounded upstream text is capped.
    envelope: Any = json.loads(block.text)
    assert envelope["error"]["code"] == "UPSTREAM_ERROR"
    assert len(envelope["error"]["message"]) < 5_000
    assert len(block.text) < 1_000
    assert "[CAM: response truncated" in envelope["error"]["message"]
