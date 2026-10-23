"""OAuth2.1 connect flows: plain admin-entered servers and MCP-spec discovery.

Discovery follows RFC 9728 (protected resource) → RFC 8414 (AS metadata) →
optional RFC 7591 dynamic client registration. Authorization uses the code
flow with PKCE S256. Tokens are encrypted with the org data key and stored on
the connection; the refresh path takes `SELECT … FOR UPDATE` so concurrent
callers wait for one refresh.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlencode, urlparse

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from cam.connections.crypto import MasterKeyRing, encrypt_secret, get_default_key_ring
from cam.connections.orgs import encrypt_for_org, get_org_data_key
from cam.db.models import Connection, OAuthState, Service
from cam.upstream.errors import UpstreamError
from cam.upstream.oauth_http import fetch_json, post_form, post_json
from cam.upstream.url import UrlPolicy

STATE_TTL = timedelta(minutes=10)
REFRESH_LEEWAY_S = 60
DEFAULT_SCOPES = ""
WELL_KNOWN_RESOURCE = "/.well-known/oauth-protected-resource"
WELL_KNOWN_AS = "/.well-known/oauth-authorization-server"

# Callback used to drop a pooled MCP session after a credential change:
# `invalidate(service_id, connection_id)`. Kept as a parameter so this module
# never has to import the session pool (or FastAPI app state).
InvalidateFn = Callable[[str, str], Awaitable[None]]


class OAuthFlowError(Exception):
    """Discovery, registration, authorize or token exchange failed."""


@dataclass(frozen=True, slots=True)
class OAuthEndpoints:
    authorization_endpoint: str
    token_endpoint: str
    client_id: str
    scopes: str
    registration_endpoint: str | None = None
    client_secret: str | None = None


def _as_dict(payload: Any, *, what: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise OAuthFlowError(f"{what} is not a JSON object")
    return cast("dict[str, Any]", payload)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items = cast("list[Any]", value)
    return [str(item) for item in items]


def _resource_metadata_urls(service_url: str) -> list[str]:
    parsed = urlparse(service_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    urls = [origin + WELL_KNOWN_RESOURCE]
    path = parsed.path.rstrip("/")
    if path:
        urls.append(f"{origin}{path}{WELL_KNOWN_RESOURCE}")
    return urls


def _as_metadata_url(issuer: str) -> str:
    parsed = urlparse(issuer.rstrip("/") or "")
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    if not path:
        return base + WELL_KNOWN_AS
    return f"{base}{WELL_KNOWN_AS}{path}"


def _require_str(config: dict[str, Any], key: str) -> str:
    value = str(config.get(key) or "").strip()
    if not value:
        raise OAuthFlowError(f"OAuth config is missing {key}")
    return value


async def _load_policy(
    session: AsyncSession, *, service: Service, allowlist: tuple[str, ...]
) -> UrlPolicy:
    del session
    if service.tier != "personal":
        return UrlPolicy(restricted=False)
    return UrlPolicy(restricted=True, allowlist=allowlist)


async def _load_allowlist(session: AsyncSession, service: Service) -> tuple[str, ...]:
    from cam.connections.url_policy import load_url_allowlist

    if service.tier != "personal":
        return ()
    return await load_url_allowlist(session, org_id=service.org_id)


async def ensure_oauth_endpoints(
    session: AsyncSession,
    *,
    service: Service,
    base_url: str,
    ring: MasterKeyRing | None = None,
) -> OAuthEndpoints:
    """Return cached endpoints, or discover/register (MCP-spec) and cache them."""
    key_ring = ring if ring is not None else get_default_key_ring()
    config = dict(service.auth_config or {})
    mode = str(config.get("oauth") or "pending")
    allowlist = await _load_allowlist(session, service)
    policy = await _load_policy(session, service=service, allowlist=allowlist)

    if mode == "plain":
        return OAuthEndpoints(
            authorization_endpoint=_require_str(config, "authorization_endpoint"),
            token_endpoint=_require_str(config, "token_endpoint"),
            client_id=_require_str(config, "client_id"),
            scopes=str(config.get("scopes") or DEFAULT_SCOPES),
            registration_endpoint=None,
            client_secret=None,
        )

    if (
        mode in ("mcp", "discovered")
        and config.get("authorization_endpoint")
        and config.get("token_endpoint")
        and config.get("client_id")
    ):
        return OAuthEndpoints(
            authorization_endpoint=_require_str(config, "authorization_endpoint"),
            token_endpoint=_require_str(config, "token_endpoint"),
            client_id=_require_str(config, "client_id"),
            scopes=str(config.get("scopes") or DEFAULT_SCOPES),
            registration_endpoint=str(config.get("registration_endpoint") or "") or None,
        )

    issuer = ""
    scopes = DEFAULT_SCOPES
    registration_endpoint: str | None = None
    authorization_endpoint = ""
    token_endpoint = ""
    for url in _resource_metadata_urls(service.url):
        try:
            resource = _as_dict(
                await fetch_json(url, url_policy=policy), what="protected resource metadata"
            )
        except UpstreamError:
            continue
        servers = resource.get("authorization_servers")
        if isinstance(servers, list) and servers:
            server_list = cast("list[Any]", servers)
            first = server_list[0]
            if isinstance(first, str) and first:
                issuer = first.rstrip("/")
                raw_scopes = _string_list(resource.get("scopes_supported"))
                if raw_scopes:
                    scopes = " ".join(raw_scopes)
                break
    if not issuer:
        raise OAuthFlowError(
            "could not discover an OAuth authorization server from protected resource metadata"
        )

    try:
        as_meta = _as_dict(
            await fetch_json(_as_metadata_url(issuer), url_policy=policy),
            what="authorization server metadata",
        )
    except UpstreamError as exc:
        raise OAuthFlowError(f"could not fetch authorization server metadata: {exc}") from exc

    authorization_endpoint = str(as_meta.get("authorization_endpoint") or "").strip()
    token_endpoint = str(as_meta.get("token_endpoint") or "").strip()
    if not authorization_endpoint or not token_endpoint:
        raise OAuthFlowError("authorization server metadata is missing endpoint URLs")
    registration_endpoint = str(as_meta.get("registration_endpoint") or "").strip() or None
    as_scopes = _string_list(as_meta.get("scopes_supported"))
    if as_scopes and scopes == DEFAULT_SCOPES:
        scopes = " ".join(as_scopes)

    redirect_uri = f"{base_url.rstrip('/')}/api/oauth/callback"
    client_id = str(config.get("client_id") or "").strip()
    client_secret: str | None = None
    if not client_id and registration_endpoint:
        try:
            registered = _as_dict(
                await post_json(
                    registration_endpoint,
                    {
                        "redirect_uris": [redirect_uri],
                        "token_endpoint_auth_method": "client_secret_basic",
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "client_name": "CAM",
                    },
                    url_policy=policy,
                ),
                what="dynamic client registration",
            )
        except UpstreamError as exc:
            raise OAuthFlowError(f"dynamic client registration failed: {exc}") from exc
        client_id = str(registered.get("client_id") or "").strip()
        raw_secret = registered.get("client_secret")
        if isinstance(raw_secret, str) and raw_secret:
            client_secret = raw_secret
    if not client_id:
        raise OAuthFlowError("no client_id available for OAuth (registration unsupported)")

    config.update(
        {
            "oauth": "mcp",
            "authorization_endpoint": authorization_endpoint,
            "token_endpoint": token_endpoint,
            "client_id": client_id,
            "scopes": scopes,
            "registration_endpoint": registration_endpoint or "",
        }
    )
    service.auth_config = config
    if client_secret is not None:
        key_version, data_key = await get_org_data_key(session, service.org_id, ring=key_ring)
        service.auth_secret_enc = encrypt_secret(
            client_secret.encode("utf-8"), data_key, key_version=key_version
        )
    await session.flush()
    return OAuthEndpoints(
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        client_id=client_id,
        scopes=scopes,
        registration_endpoint=registration_endpoint,
        client_secret=client_secret,
    )


async def _stored_client_secret(
    session: AsyncSession, *, service: Service, ring: MasterKeyRing | None = None
) -> str | None:
    if service.auth_secret_enc is None:
        return None
    from cam.connections.orgs import decrypt_for_org

    key_ring = ring if ring is not None else get_default_key_ring()
    result = await decrypt_for_org(session, service.org_id, service.auth_secret_enc, ring=key_ring)
    return result.plaintext.decode("utf-8")


async def _client_secret_for_token(
    session: AsyncSession, *, service: Service, endpoints: OAuthEndpoints
) -> str | None:
    if endpoints.client_secret is not None:
        return endpoints.client_secret
    mode = str((service.auth_config or {}).get("oauth") or "")
    if mode == "plain" or service.auth_secret_enc is not None:
        return await _stored_client_secret(session, service=service)
    return None


def build_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _connection_user_id_for(service: Service, state_user_id: Any) -> Any:
    if service.tier == "global":
        return None
    if service.tier == "personal":
        return service.owner_user_id
    return state_user_id


async def start_authorize(
    session: AsyncSession,
    *,
    service: Service,
    user_id: Any,
    base_url: str,
    ring: MasterKeyRing | None = None,
) -> str:
    endpoints = await ensure_oauth_endpoints(session, service=service, base_url=base_url, ring=ring)
    verifier, challenge = build_pkce_pair()
    state = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    row = OAuthState(
        org_id=service.org_id,
        user_id=user_id,
        service_id=service.id,
        state=state,
        pkce_verifier=verifier,
        expires_at=now + STATE_TTL,
    )
    session.add(row)
    await session.flush()
    redirect_uri = f"{base_url.rstrip('/')}/api/oauth/callback"
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": endpoints.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if endpoints.scopes:
        params["scope"] = endpoints.scopes
    joiner = "&" if "?" in endpoints.authorization_endpoint else "?"
    return f"{endpoints.authorization_endpoint}{joiner}{urlencode(params)}"


async def _consume_state(session: AsyncSession, state: str) -> OAuthState:
    now = datetime.now(UTC)
    result = await session.execute(
        select(OAuthState).where(OAuthState.state == state).with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise OAuthFlowError("unknown OAuth state")
    if row.expires_at <= now:
        await session.delete(row)
        await session.flush()
        raise OAuthFlowError("OAuth state expired")
    return row


async def _store_tokens(
    session: AsyncSession,
    *,
    service: Service,
    connection: Connection,
    token: dict[str, Any],
    ring: MasterKeyRing | None = None,
) -> None:
    key_ring = ring if ring is not None else get_default_key_ring()
    access_token = str(token.get("access_token") or "").strip()
    if not access_token:
        raise OAuthFlowError("token response is missing access_token")
    token_type = str(token.get("token_type") or "Bearer")
    payload = json.dumps({"access_token": access_token, "token_type": token_type})
    blob, key_version = await encrypt_for_org(session, service.org_id, payload, ring=key_ring)
    connection.secret_enc = blob
    connection.key_version = key_version
    refresh_token = token.get("refresh_token")
    if isinstance(refresh_token, str) and refresh_token:
        refresh_blob, refresh_version = await encrypt_for_org(
            session, service.org_id, refresh_token, ring=key_ring
        )
        connection.refresh_enc = refresh_blob
        connection.key_version = refresh_version
    expires_in = token.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        connection.expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))
    else:
        connection.expires_at = None
    connection.status = "connected"
    if service.health == "needs_reconnect":
        service.health = "healthy"
    await session.flush()


async def _token_request(
    session: AsyncSession,
    *,
    service: Service,
    endpoints: OAuthEndpoints,
    data: dict[str, str],
    ring: MasterKeyRing | None = None,
) -> dict[str, Any]:
    allowlist = await _load_allowlist(session, service)
    policy = await _load_policy(session, service=service, allowlist=allowlist)
    client_secret = await _client_secret_for_token(session, service=service, endpoints=endpoints)
    form = dict(data)
    form["client_id"] = endpoints.client_id
    basic_auth: tuple[str, str] | None = None
    if client_secret:
        auth_method = str((service.auth_config or {}).get("token_auth") or "client_secret_basic")
        if auth_method == "client_secret_post":
            form["client_secret"] = client_secret
        else:
            basic_auth = (endpoints.client_id, client_secret)
    try:
        return await post_form(
            endpoints.token_endpoint, form, url_policy=policy, basic_auth=basic_auth
        )
    except UpstreamError as exc:
        raise OAuthFlowError(f"token request failed: {exc}") from exc


async def complete_authorize(
    session: AsyncSession,
    *,
    service: Service,
    code: str,
    state: str,
    base_url: str,
    ring: MasterKeyRing | None = None,
    connection_user_id: Any = None,
    invalidate: InvalidateFn | None = None,
) -> Connection:
    del connection_user_id
    endpoints = await ensure_oauth_endpoints(session, service=service, base_url=base_url, ring=ring)
    row = await _consume_state(session, state)
    if row.service_id != service.id:
        raise OAuthFlowError("OAuth state does not match this service")
    verifier = row.pkce_verifier
    state_user_id = row.user_id
    await session.delete(row)
    redirect_uri = f"{base_url.rstrip('/')}/api/oauth/callback"
    token = await _token_request(
        session,
        service=service,
        endpoints=endpoints,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        ring=ring,
    )
    user_id = _connection_user_id_for(service, state_user_id)
    connection = await _get_or_create_connection(
        session, service=service, user_id=user_id, ring=ring
    )
    await _store_tokens(session, service=service, connection=connection, token=token, ring=ring)
    if invalidate is not None:
        # New tokens were stored; a pooled MCP session still carries the old
        # Authorization header (review R10).
        await invalidate(str(service.id), str(connection.id))
    return connection


async def delete_oauth_states(
    session: AsyncSession,
    *,
    service_id: Any | None = None,
    user_id: Any | None = None,
) -> int:
    """Delete pending OAuth state rows for a service and/or user.

    Call when a user is deactivated or a service is disabled/deleted so a
    stale authorize round-trip cannot complete and (re)activate a connection
    (review R12).
    """
    if service_id is None and user_id is None:
        raise ValueError("provide service_id and/or user_id")
    conditions: list[Any] = []
    if service_id is not None:
        conditions.append(OAuthState.service_id == service_id)
    if user_id is not None:
        conditions.append(OAuthState.user_id == user_id)
    result = cast("CursorResult[Any]", await session.execute(delete(OAuthState).where(*conditions)))
    await session.flush()
    return int(result.rowcount or 0)


async def _get_or_create_connection(
    session: AsyncSession,
    *,
    service: Service,
    user_id: Any,
    ring: MasterKeyRing | None = None,
) -> Connection:
    key_ring = ring if ring is not None else get_default_key_ring()
    result = await session.execute(
        select(Connection).where(
            Connection.org_id == service.org_id,
            Connection.service_id == service.id,
            Connection.user_id == user_id,
        )
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        key_version, _data_key = await get_org_data_key(session, service.org_id, ring=key_ring)
        connection = Connection(
            org_id=service.org_id,
            service_id=service.id,
            user_id=user_id,
            key_version=key_version,
            status="connected",
        )
        session.add(connection)
        await session.flush()
    return connection


async def refresh_if_needed(
    session: AsyncSession,
    *,
    service: Service,
    connection: Connection,
    base_url: str,
    ring: MasterKeyRing | None = None,
    invalidate: InvalidateFn | None = None,
) -> bool:
    """Refresh when `expires_at` is within 60s, under FOR UPDATE.

    Returns ``True`` when new tokens were stored (the caller should drop any
    pooled MCP session so the new access token is sent — review R10), else
    ``False``.
    """
    now = datetime.now(UTC)
    needs_refresh = connection.expires_at is not None and connection.expires_at <= now + timedelta(
        seconds=REFRESH_LEEWAY_S
    )
    if not needs_refresh:
        return False

    # Reload the locked row from the database (`populate_existing`): without it
    # the identity map returns the stale object (old expiry/refresh token), the
    # post-lock re-check reads stale values, and two callers can both exchange a
    # rotating refresh token — the loser then wrongly flags a healthy
    # connection as `needs_reconnect` (review R09).
    locked = (
        await session.execute(
            select(Connection)
            .where(Connection.id == connection.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    # Re-check against the freshly locked row.
    if locked.status != "connected":
        return False
    if locked.expires_at is None or locked.expires_at > datetime.now(UTC) + timedelta(
        seconds=REFRESH_LEEWAY_S
    ):
        return False

    if locked.refresh_enc is None:
        locked.status = "needs_reconnect"
        service.health = "needs_reconnect"
        await session.commit()
        raise OAuthFlowError("OAuth refresh token is missing; reconnect required")

    endpoints = await ensure_oauth_endpoints(session, service=service, base_url=base_url, ring=ring)
    from cam.connections.orgs import decrypt_for_org

    key_ring = ring if ring is not None else get_default_key_ring()
    refresh_plain = (
        await decrypt_for_org(session, service.org_id, locked.refresh_enc, ring=key_ring)
    ).plaintext.decode("utf-8")
    try:
        token = await _token_request(
            session,
            service=service,
            endpoints=endpoints,
            data={"grant_type": "refresh_token", "refresh_token": refresh_plain},
            ring=ring,
        )
    except OAuthFlowError:
        locked.status = "needs_reconnect"
        service.health = "needs_reconnect"
        await session.commit()
        raise
    await _store_tokens(session, service=service, connection=locked, token=token, ring=ring)
    await session.commit()
    if connection is not locked:
        connection.secret_enc = locked.secret_enc
        connection.refresh_enc = locked.refresh_enc
        connection.expires_at = locked.expires_at
        connection.status = locked.status
        connection.key_version = locked.key_version
    if invalidate is not None:
        await invalidate(str(service.id), str(locked.id))
    return True
