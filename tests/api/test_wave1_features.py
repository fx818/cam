"""Wave-1 features: usage notes, example invocations, NOT_FOUND hints,
attention digest, audit forensics filters + export."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from api.helpers import ApiEnv, auth_headers, insert_audit
from cam.api.attention import admin_attention_items
from cam.api.queries import (
    AuditFilters,
    SessionIdentity,
    admin_forensic_export,
    build_audit_csv,
)
from cam.connections.orgs import encrypt_for_org, ensure_org
from cam.connections.tokens import AuthenticatedIdentity, create_token
from cam.db.models import Connection, Org, Service, Tool, User
from cam.db.session import create_session_factory
from cam.registry.examples import sample_invocation
from cam.registry.resync import UpstreamToolSpec, apply_diff

TEST_DATABASE_URL = "postgresql+asyncpg://cam:cam@localhost:5432/cam_test"


# --- examples module ---------------------------------------------------------


def test_sample_from_simple_schema() -> None:
    schema = {
        "type": "object",
        "required": ["dept_id", "date"],
        "properties": {
            "dept_id": {"type": "integer"},
            "date": {"type": "string", "format": "date"},
        },
    }
    sample = sample_invocation(schema)
    assert sample is not None
    assert sample["dept_id"] == 1
    assert sample["date"] == "2026-01-01"


def test_sample_respects_required_default() -> None:
    sample = sample_invocation(
        {
            "type": "object",
            "required": ["limit"],
            "properties": {"limit": {"type": "integer", "default": 50}},
        }
    )
    assert sample == {"limit": 50}


def test_optional_defaults_are_included() -> None:
    sample = sample_invocation(
        {"type": "object", "properties": {"limit": {"type": "integer", "default": 50}}}
    )
    assert sample == {"limit": 50}


def test_impossible_pattern_returns_none() -> None:
    sample = sample_invocation(
        {
            "type": "object",
            "required": ["code"],
            "properties": {"code": {"type": "string", "pattern": "[A-Z]{12}"}},
        }
    )
    assert sample is None


def test_override_wins() -> None:
    sample = sample_invocation(
        {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}},
        example_override={"x": 7},
    )
    assert sample == {"x": 7}


def test_sample_resolves_refs() -> None:
    schema = {
        "$defs": {"Num": {"type": "number"}},
        "type": "object",
        "required": ["amount"],
        "properties": {"amount": {"$ref": "#/$defs/Num"}},
    }
    sample = sample_invocation(schema)
    assert sample is not None
    assert isinstance(sample["amount"], (int, float))


async def test_apply_diff_seeds_examples(db_session: AsyncSession) -> None:
    org_row = await ensure_org(db_session)
    suffix = uuid.uuid4().hex[:8]
    service = Service(
        org_id=org_row.id,
        tier="global",
        owner_user_id=None,
        slug=f"ex-{suffix}",
        name="Examples",
        kind="mcp",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="none",
    )
    db_session.add(service)
    await db_session.flush()
    spec = UpstreamToolSpec(
        name="run_query",
        description="run",
        input_schema={
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
        },
    )
    added, _updated, _removed = await apply_diff(db_session, service, [spec])
    assert added == ["run_query"]
    tool = (
        await db_session.execute(select(Tool).where(Tool.service_id == service.id))
    ).scalar_one()
    assert tool.example_invocation == {"text": "example-value"}


# --- tool metadata API -------------------------------------------------------


async def test_tool_patch_usage_note_and_example(
    api: ApiEnv,
    admin_token: str,
    user_token: str,
    personal_tool: Tool,
    db_session: AsyncSession,
) -> None:
    headers = await auth_headers(api.client, user_token)
    response = await api.client.patch(
        f"/api/tools/{personal_tool.id}",
        json={"usage_note": "dates are IST", "example_invocation": {"text": "hi"}},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()["tool"]
    assert payload["usage_note"] == "dates are IST"
    assert payload["example_invocation"] == {"text": "hi"}
    tool = (
        await db_session.execute(
            select(Tool)
            .where(Tool.id == personal_tool.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert tool.usage_note == "dates are IST"
    assert tool.example_invocation == {"text": "hi"}


async def test_tool_patch_clears_metadata(
    api: ApiEnv, user_token: str, personal_tool: Tool
) -> None:
    headers = await auth_headers(api.client, user_token)
    await api.client.patch(
        f"/api/tools/{personal_tool.id}",
        json={"usage_note": "note", "example_invocation": {"a": 1}},
        headers=headers,
    )
    response = await api.client.patch(
        f"/api/tools/{personal_tool.id}",
        json={"usage_note": None, "example_invocation": None},
        headers=headers,
    )
    assert response.status_code == 200
    payload = response.json()["tool"]
    assert payload["usage_note"] is None
    assert payload["example_invocation"] is None


# --- NOT_FOUND hint ----------------------------------------------------------


async def testclosest_tool_hint_never_leaves_access(
    db_session: AsyncSession,
    api_user: User,
) -> None:
    org_row = await ensure_org(db_session)
    prefix = uuid.uuid4().hex[:6]
    visible_service = Service(
        org_id=org_row.id,
        tier="global",
        owner_user_id=None,
        slug=f"vis-{prefix}",
        name="Visible",
        kind="mcp",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="none",
        visibility="all",
    )
    hidden_service = Service(
        org_id=org_row.id,
        tier="global",
        owner_user_id=None,
        slug=f"hid-{prefix}",
        name="Hidden",
        kind="mcp",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="none",
        visibility="restricted",
    )
    db_session.add(visible_service)
    db_session.add(hidden_service)
    await db_session.flush()
    tool_name = f"run_{prefix}"
    db_session.add_all(
        [
            Tool(
                org_id=org_row.id,
                service_id=visible_service.id,
                name=tool_name,
                upstream_name=tool_name,
                description="d",
                input_schema={"type": "object"},
                enabled=True,
                status="active",
            ),
            Tool(
                org_id=org_row.id,
                service_id=hidden_service.id,
                name=tool_name,
                upstream_name=tool_name,
                description="d",
                input_schema={"type": "object"},
                enabled=True,
                status="active",
            ),
        ]
    )
    _raw, token_row = await create_token(
        db_session, org_id=org_row.id, user_id=api_user.id, label="hint"
    )
    await db_session.commit()

    from cam.invoke.pipeline import closest_tool_hint

    api_user.org_id = org_row.id
    identity = AuthenticatedIdentity(org_id=org_row.id, user=api_user, token=token_row)
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        factory = create_session_factory(engine)
        hint = await closest_tool_hint(
            factory,  # type: ignore[arg-type]
            identity,
            f"global/vis-{prefix}/rn_{prefix}",
        )
        hidden_id = f"global/hid-{prefix}/{tool_name}"
        if hint is not None:
            assert hint != hidden_id
            assert hint.startswith(f"global/vis-{prefix}/")
    finally:
        await engine.dispose()


# --- attention digest --------------------------------------------------------


async def test_attention_reports_problem_categories(
    db_session: AsyncSession, api_admin: User
) -> None:
    org_row = await ensure_org(db_session)
    suffix = uuid.uuid4().hex[:8]
    service = Service(
        org_id=org_row.id,
        tier="global",
        owner_user_id=None,
        slug=f"att-{suffix}",
        name="AttSvc",
        kind="mcp",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="bearer",
        auth_config={},
        health="failing",
        status="active",
    )
    db_session.add(service)
    await db_session.flush()
    secret_blob, _ = await encrypt_for_org(db_session, org_row.id, "attention-cred")
    db_session.add(
        Tool(
            org_id=org_row.id,
            service_id=service.id,
            name="stuck-tool",
            upstream_name="stuck-tool",
            description="d",
            input_schema={"type": "object"},
            enabled=False,
            status="pending_review",
            created_at=datetime.now(tz=UTC) - timedelta(days=6),
        )
    )
    db_session.add(
        Connection(
            org_id=org_row.id,
            service_id=service.id,
            user_id=None,
            status="connected",
            secret_enc=secret_blob,
            last_rotated_at=datetime.now(tz=UTC) - timedelta(days=200),
        )
    )
    db_session.add(
        Connection(
            org_id=org_row.id,
            service_id=service.id,
            user_id=api_admin.id,
            status="needs_reconnect",
        )
    )
    await db_session.commit()

    org_obj = await db_session.get(Org, org_row.id)
    assert org_obj is not None
    identity = SessionIdentity(user=api_admin, org=org_obj, org_id=org_row.id)
    items = await admin_attention_items(db_session, identity)
    labels = [item["label"] for item in items]
    assert any('"stuck-tool" awaits approval' in label for label in labels)
    assert any("AttSvc: upstream failing" in label for label in labels)
    stale = [item for item in items if item["kind"] == "stale_credential"]
    assert any("AttSvc" in item["label"] for item in stale)
    reconnect = [item for item in items if item["kind"] == "reconnect"]
    assert len(reconnect) >= 1


async def test_attention_flags_never_rotated(db_session: AsyncSession, api_admin: User) -> None:
    org_row = await ensure_org(db_session)
    service = Service(
        org_id=org_row.id,
        tier="global",
        owner_user_id=None,
        slug=f"stale-{uuid.uuid4().hex[:8]}",
        name="StaleSvc",
        kind="mcp",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="bearer",
    )
    db_session.add(service)
    await db_session.flush()
    stale_blob, _ = await encrypt_for_org(db_session, org_row.id, "stale-cred")
    db_session.add(
        Connection(
            org_id=org_row.id,
            service_id=service.id,
            user_id=None,
            status="connected",
            secret_enc=stale_blob,
            last_rotated_at=None,
        )
    )
    await db_session.commit()
    org_obj = await db_session.get(Org, org_row.id)
    assert org_obj is not None
    items = await admin_attention_items(
        db_session, SessionIdentity(user=api_admin, org=org_obj, org_id=org_row.id)
    )
    assert any("never rotated" in item["label"] for item in items)


# --- audit forensics ---------------------------------------------------------


async def test_forensic_filters_and_export(db_session: AsyncSession, api_admin: User) -> None:
    org_row = await ensure_org(db_session)
    service = Service(
        org_id=org_row.id,
        tier="global",
        owner_user_id=None,
        slug=f"aud-{uuid.uuid4().hex[:8]}",
        name="AuditSvc",
        kind="mcp",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="none",
    )
    db_session.add(service)
    await db_session.flush()
    await insert_audit(
        db_session,
        org_id=org_row.id,
        user_id=api_admin.id,
        service_id=service.id,
        agent_name="probe-agent",
        args={"needle": "find-me-999"},
    )
    await db_session.commit()

    org_obj = await db_session.get(Org, org_row.id)
    assert org_obj is not None
    identity = SessionIdentity(user=api_admin, org=org_obj, org_id=org_row.id)

    matching = await admin_forensic_export(
        db_session,
        identity,
        AuditFilters(
            scope="all",
            agent=None,
            service_ref=None,
            status=None,
            date_from=None,
            date_to=None,
            args_query="find-me-999",
        ),
    )
    assert any("find-me-999" in str(entry["args"]) for entry in matching)

    misses = await admin_forensic_export(
        db_session,
        identity,
        AuditFilters(
            scope="all",
            agent=None,
            service_ref=None,
            status=None,
            date_from=None,
            date_to=None,
            args_query="absent-string",
        ),
    )
    assert misses == []

    csv_text, filename = build_audit_csv(matching)
    assert filename.startswith("cam-audit-")
    assert "find-me-999" in csv_text


def test_csv_escapes_quotes_and_commas() -> None:
    csv_text, filename = build_audit_csv(
        [
            {
                "ts": "2026-10-05T10:00:00+00:00",
                "action": "invoke",
                "status": "ok",
                "error_code": None,
                "actor_email": "a@example.com",
                "token_label": None,
                "agent_name": None,
                "agent_version": None,
                "service_ref": "global/x",
                "tool_name": 'say "hi", ok',
                "latency_ms": 5,
                "response_bytes": 10,
                "truncated": False,
                "args": {"q": "a,b"},
            }
        ]
    )
    assert '"say ""hi"", ok"' in csv_text
    assert filename.endswith(".csv")
