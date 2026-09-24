"""Personal services under <handle>/<service>/<tool>."""

from __future__ import annotations

import uuid
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cam.access import (
    ServiceDisabled,
    ServiceNotFound,
    list_services,
    list_tools,
    resolve_connection,
    resolve_service,
    resolve_tool,
)
from cam.connections.orgs import decrypt_for_org, ensure_org
from cam.connections.tokens import AuthenticatedIdentity, create_token
from cam.connections.url_policy import add_url_allowlist
from cam.db.models import Connection, Service, Tool, User
from cam.registry.services import (
    ServiceAlreadyExists,
    ServiceError,
    ServiceStatus,
    register_global_mcp,
    register_personal_mcp,
    set_service_status,
)


async def _make_identity(
    db_session: AsyncSession, suffix: str, *, org_slug: str = "indiamart"
) -> AuthenticatedIdentity:
    org = await ensure_org(db_session, slug=org_slug)
    user = User(
        org_id=org.id,
        email=f"pers-{suffix}@example.com",
        handle=f"pers{suffix}",
        display_name="Personal Test",
        password_hash="x",
        role="user",
        kind="person",
    )
    db_session.add(user)
    await db_session.flush()
    _raw, token = await create_token(db_session, org_id=org.id, user_id=user.id, label="pers")
    return AuthenticatedIdentity(org_id=org.id, user=user, token=token)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _register(
    db_session: AsyncSession, owner: User, *, slug: str | None = None, credential: str | None = None
) -> tuple[Service, Connection]:
    return await register_personal_mcp(
        db_session,
        owner=owner,
        slug=slug or _unique("mine"),
        name="My MCP",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="bearer" if credential else "none",
        credential=credential,
        description="personal service",
    )


async def _add_tool(
    db_session: AsyncSession, org_id: uuid.UUID, service: Service, name: str
) -> Tool:
    tool = Tool(
        org_id=org_id,
        service_id=service.id,
        name=name,
        upstream_name=name,
        description=f"tool {name}",
        input_schema={"type": "object", "properties": {}},
    )
    db_session.add(tool)
    await db_session.flush()
    return tool


async def test_register_personal_mcp_binds_owner_and_credential(
    db_session: AsyncSession,
) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "a")
    secret = f"tok-{uuid.uuid4().hex}"
    service, connection = await _register(db_session, identity.user, credential=secret)
    assert service.tier == "personal"
    assert service.owner_user_id == identity.user.id
    assert service.org_id == identity.org_id
    assert service.visibility == "all"
    assert service.status == "active"
    assert connection.user_id == identity.user.id
    assert connection.status == "connected"
    assert connection.secret_enc is not None
    assert secret.encode() not in connection.secret_enc
    decrypted = await decrypt_for_org(db_session, identity.org_id, connection.secret_enc)
    assert decrypted.plaintext == secret.encode()


async def test_owner_resolves_and_lists_personal_service(db_session: AsyncSession) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "b")
    service, _connection = await _register(db_session, identity.user)
    await _add_tool(db_session, identity.org_id, service, "send")
    ref = f"{identity.user.handle}/{service.slug}"

    resolved = await resolve_service(db_session, identity, ref)
    assert resolved.service.id == service.id
    assert resolved.ref == ref
    assert resolved.owner_handle == identity.user.handle

    resolved_service, tools = await list_tools(db_session, identity, ref)
    assert resolved_service.ref == ref
    assert [tool.name for tool in tools] == ["send"]

    resolved_tool = await resolve_tool(db_session, identity, f"{ref}/send")
    assert resolved_tool.id == f"{ref}/send"
    assert resolved_tool.service_ref == ref

    summaries = await list_services(db_session, identity)
    summary = next(s for s in summaries if s.id == ref)
    assert summary.tier == "personal"
    assert summary.owner == identity.user.handle
    assert summary.tool_count == 1


async def test_non_owner_cannot_reach_personal_service(db_session: AsyncSession) -> None:
    owner = await _make_identity(db_session, uuid.uuid4().hex[:8] + "c")
    stranger = await _make_identity(db_session, uuid.uuid4().hex[:8] + "d")
    service, _connection = await _register(db_session, owner.user)
    await _add_tool(db_session, owner.org_id, service, "send")
    ref = f"{owner.user.handle}/{service.slug}"

    with pytest.raises(ServiceNotFound):
        await resolve_service(db_session, stranger, ref)
    with pytest.raises(ServiceNotFound):
        await resolve_tool(db_session, stranger, f"{ref}/send")
    with pytest.raises(ServiceNotFound):
        await list_tools(db_session, stranger, ref)
    summaries = await list_services(db_session, stranger)
    assert ref not in {s.id for s in summaries}


async def test_unknown_owner_and_bad_refs_not_found(db_session: AsyncSession) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "e")
    service, _connection = await _register(db_session, identity.user)
    bad_refs = [
        f"ghost-{uuid.uuid4().hex[:6]}/{service.slug}",
        f"{identity.user.handle}/ghost-{uuid.uuid4().hex[:6]}",
        identity.user.handle,
        f"{identity.user.handle}/{service.slug}/extra",
        f"global/{service.slug}",
    ]
    for ref in bad_refs:
        with pytest.raises(ServiceNotFound):
            await resolve_service(db_session, identity, ref)
    with pytest.raises(ServiceNotFound):
        await resolve_tool(db_session, identity, f"ghost/{uuid.uuid4().hex[:6]}/tool")


async def test_cross_org_caller_cannot_reach_personal_service(
    db_session: AsyncSession,
) -> None:
    owner = await _make_identity(db_session, uuid.uuid4().hex[:8] + "f")
    outsider = await _make_identity(
        db_session, uuid.uuid4().hex[:8] + "g", org_slug=f"other-{uuid.uuid4().hex[:8]}"
    )
    service, _connection = await _register(db_session, owner.user)
    await _add_tool(db_session, owner.org_id, service, "send")
    ref = f"{owner.user.handle}/{service.slug}"

    with pytest.raises(ServiceNotFound):
        await resolve_service(db_session, outsider, ref)
    with pytest.raises(ServiceNotFound):
        await resolve_tool(db_session, outsider, f"{ref}/send")
    summaries = await list_services(db_session, outsider)
    assert ref not in {s.id for s in summaries}


async def test_disable_and_enable_personal_service(db_session: AsyncSession) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "h")
    service, _connection = await _register(db_session, identity.user)
    await _add_tool(db_session, identity.org_id, service, "send")
    ref = f"{identity.user.handle}/{service.slug}"

    disabled = await set_service_status(db_session, ref=ref, status="disabled")
    assert disabled.status == "disabled"
    with pytest.raises(ServiceDisabled):
        await resolve_service(db_session, identity, ref)
    with pytest.raises(ServiceDisabled):
        await resolve_tool(db_session, identity, f"{ref}/send")
    summaries = await list_services(db_session, identity)
    assert ref not in {s.id for s in summaries}

    enabled = await set_service_status(db_session, ref=ref, status="active")
    assert enabled.status == "active"
    resolved = await resolve_service(db_session, identity, ref)
    assert resolved.service.id == service.id


async def test_set_service_status_unknown_refs(db_session: AsyncSession) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "i")
    service, _connection = await _register(db_session, identity.user)
    with pytest.raises(ServiceError, match="no service with id"):
        await set_service_status(db_session, ref=f"{identity.user.handle}/ghost", status="disabled")
    with pytest.raises(ServiceError, match="no user with handle"):
        await set_service_status(db_session, ref=f"ghosthandle/{service.slug}", status="disabled")
    with pytest.raises(ServiceError, match="status"):
        await set_service_status(
            db_session,
            ref=f"{identity.user.handle}/{service.slug}",
            status=cast(ServiceStatus, "deleted"),
        )


async def test_resolve_connection_personal_uses_owner_credential(
    db_session: AsyncSession,
) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "j")
    service, connection = await _register(db_session, identity.user, credential="secret")
    ref = f"{identity.user.handle}/{service.slug}"
    resolved = await resolve_connection(db_session, service, ref=ref)
    assert resolved.id == connection.id
    assert resolved.user_id == identity.user.id

    connection.status = "disabled"
    await db_session.flush()
    with pytest.raises(ServiceDisabled):
        await resolve_connection(db_session, service, ref=ref)
    connection.status = "connected"
    await db_session.flush()

    await db_session.delete(connection)
    await db_session.flush()
    with pytest.raises(ServiceDisabled):
        await resolve_connection(db_session, service, ref=ref)


async def test_duplicate_personal_slug_rules(db_session: AsyncSession) -> None:
    owner = await _make_identity(db_session, uuid.uuid4().hex[:8] + "k")
    other = await _make_identity(db_session, uuid.uuid4().hex[:8] + "l")
    slug = _unique("sharedslug")
    await _register(db_session, owner.user, slug=slug)
    with pytest.raises(ServiceAlreadyExists):
        await _register(db_session, owner.user, slug=slug)
    await _register(db_session, other.user, slug=slug)
    global_service, _ = await register_global_mcp(
        db_session,
        slug=slug,
        name="Global Same",
        url="https://example.com/mcp",
        transport="streamable-http",
        auth_type="none",
    )
    global_resolved = await resolve_service(db_session, owner, f"global/{slug}")
    assert global_resolved.service.id == global_service.id
    personal_ref = f"{owner.user.handle}/{slug}"
    personal_resolved = await resolve_service(db_session, owner, personal_ref)
    assert personal_resolved.ref == personal_ref


async def test_personal_registration_rejects_internal_urls(db_session: AsyncSession) -> None:
    org_slug = f"reject-{uuid.uuid4().hex[:10]}"
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "p", org_slug=org_slug)
    internal_urls = (
        "http://127.0.0.1:9/mcp",
        "http://localhost:9/mcp",
        "http://app.localhost/mcp",
        "http://169.254.169.254/latest/meta-data/",
    )
    for url in internal_urls:
        with pytest.raises(ServiceError, match="blocked"):
            await register_personal_mcp(
                db_session,
                owner=identity.user,
                slug=_unique("bad"),
                name="Bad",
                url=url,
                transport="streamable-http",
                auth_type="none",
            )


async def test_personal_registration_allowlist_exempts_loopback(
    db_session: AsyncSession,
) -> None:
    org_slug = f"allow-{uuid.uuid4().hex[:10]}"
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "q", org_slug=org_slug)
    await add_url_allowlist(db_session, pattern="127.0.0.0/8", org_slug=org_slug)
    service, connection = await register_personal_mcp(
        db_session,
        owner=identity.user,
        slug=_unique("lo"),
        name="Loopback",
        url="http://127.0.0.1:9/mcp",
        transport="streamable-http",
        auth_type="none",
    )
    assert service.tier == "personal"
    assert service.url == "http://127.0.0.1:9/mcp"
    assert connection.user_id == identity.user.id


async def test_personal_registration_resolves_dns_and_rejects_blocked_ip(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "dns")

    async def _blocked(url: str, *, policy: object, resolver: object = None) -> object:
        from cam.upstream.url import UrlPolicyError

        raise UrlPolicyError("URL host 'sneaky.example.com' resolves to blocked address")

    monkeypatch.setattr("cam.registry.services.validate_outbound_url", _blocked)
    with pytest.raises(ServiceError, match="blocked address"):
        await register_personal_mcp(
            db_session,
            owner=identity.user,
            slug=_unique("dns"),
            name="Sneaky",
            url="https://example.com/mcp",
            transport="streamable-http",
            auth_type="none",
        )


async def test_personal_slug_reserved_by_template_service(db_session: AsyncSession) -> None:
    identity = await _make_identity(db_session, uuid.uuid4().hex[:8] + "r")
    slug = _unique("reserved")
    db_session.add(
        Service(
            org_id=identity.org_id,
            tier="template",
            owner_user_id=None,
            slug=slug,
            name="Template",
            url="https://example.com/mcp",
            transport="streamable-http",
        )
    )
    await db_session.flush()
    with pytest.raises(ServiceAlreadyExists, match="reserved by a template"):
        await _register(db_session, identity.user, slug=slug)
