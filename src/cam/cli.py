import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

import click
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cam import __version__
from cam.access.shares import ShareError, share_service, unshare_service
from cam.accounts.users import (
    UserError,
    UserKind,
    UserRole,
    create_user,
    deactivate_user,
    get_user_by_email,
)
from cam.config import Settings
from cam.connections.orgs import DEFAULT_ORG_SLUG
from cam.connections.tokens import create_token
from cam.connections.url_policy import AllowlistError, add_url_allowlist, remove_url_allowlist
from cam.db.session import create_db_engine, create_session_factory
from cam.registry.services import (
    McpAuthType,
    McpTransport,
    ServiceError,
    ServiceStatus,
    ServiceVisibilityLevel,
    grant_service_visibility,
    register_global_mcp,
    register_personal_mcp,
    revoke_service_visibility,
    set_service_status,
    set_service_visibility,
)

UserKindChoice = click.Choice(["person", "team"])
TransportChoice = click.Choice(["streamable-http", "sse"])
AuthTypeChoice = click.Choice(["none", "static-header", "bearer"])


async def _with_session[T](fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
    settings = Settings()
    engine = create_db_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            result = await fn(session)
            await session.commit()
        return result
    finally:
        await engine.dispose()


def _resolve_password(password: str | None) -> str:
    if password is not None:
        if not password:
            raise click.ClickException("password must not be empty")
        return password
    return click.prompt("Password", hide_input=True, confirmation_prompt=True)


def _create_user_command(
    *,
    email: str,
    password: str | None,
    handle: str | None,
    display_name: str | None,
    kind: str,
    org_slug: str,
    role: UserRole,
) -> None:
    resolved_password = _resolve_password(password)
    try:
        user = asyncio.run(
            _with_session(
                lambda session: create_user(
                    session,
                    email=email,
                    password=resolved_password,
                    role=role,
                    kind=cast(UserKind, kind),
                    handle=handle,
                    display_name=display_name,
                    org_slug=org_slug,
                )
            )
        )
    except UserError as exc:
        raise click.ClickException(str(exc)) from exc
    except IntegrityError:
        raise click.ClickException("a user with this email or handle already exists") from None
    click.echo(f"Created {user.role} user {user.email} (handle {user.handle})")


@click.group()
@click.version_option(__version__, prog_name="cam")
def main() -> None:
    """CAM admin CLI."""


@main.command("create-admin")
@click.option("--email", required=True, help="Email address for the admin account.")
@click.option("--password", default=None, help="Password; prompted hidden if omitted.")
@click.option("--handle", default=None, help="Handle; defaults to the email local part.")
@click.option("--display-name", default=None)
@click.option("--kind", type=UserKindChoice, default="person", show_default=True)
@click.option("--org", "org_slug", default=DEFAULT_ORG_SLUG, show_default=True)
def create_admin(
    email: str,
    password: str | None,
    handle: str | None,
    display_name: str | None,
    kind: str,
    org_slug: str,
) -> None:
    """Create an admin user (use this for the first admin)."""
    _create_user_command(
        email=email,
        password=password,
        handle=handle,
        display_name=display_name,
        kind=kind,
        org_slug=org_slug,
        role="admin",
    )


@main.command("create-user")
@click.option("--email", required=True, help="Email address for the account.")
@click.option("--password", default=None, help="Password; prompted hidden if omitted.")
@click.option("--handle", default=None, help="Handle; defaults to the email local part.")
@click.option("--display-name", default=None)
@click.option("--kind", type=UserKindChoice, default="person", show_default=True)
@click.option("--org", "org_slug", default=DEFAULT_ORG_SLUG, show_default=True)
def create_user_cmd(
    email: str,
    password: str | None,
    handle: str | None,
    display_name: str | None,
    kind: str,
    org_slug: str,
) -> None:
    """Create a regular user."""
    _create_user_command(
        email=email,
        password=password,
        handle=handle,
        display_name=display_name,
        kind=kind,
        org_slug=org_slug,
        role="user",
    )


@main.command("issue-token")
@click.option("--email", required=True, help="Email of the user the token is for.")
@click.option("--label", required=True, help="Label shown in audit logs.")
def issue_token(email: str, label: str) -> None:
    """Issue a CAM token; the raw token is printed exactly once."""

    async def _issue(session: AsyncSession) -> str:
        user = await get_user_by_email(session, email)
        if user is None:
            raise click.ClickException(f"no user with email {email}")
        if user.status != "active" or user.deactivated_at is not None:
            raise click.ClickException(f"user {email} is not active")
        raw_token, _ = await create_token(session, org_id=user.org_id, user_id=user.id, label=label)
        return raw_token

    click.echo(asyncio.run(_with_session(_issue)))


@main.command("register-global-mcp")
@click.option("--slug", required=True, help="Service slug, e.g. windmill.")
@click.option("--name", required=True, help="Display name.")
@click.option("--url", required=True, help="MCP endpoint URL.")
@click.option("--transport", type=TransportChoice, required=True)
@click.option("--auth-type", type=AuthTypeChoice, default="none", show_default=True)
@click.option("--header-name", default=None, help="Header name for static-header auth.")
@click.option(
    "--header-value",
    default=None,
    help="Secret header or bearer value; prompted hidden when required.",
)
@click.option("--description", default="")
def register_global_mcp_cmd(
    slug: str,
    name: str,
    url: str,
    transport: str,
    auth_type: str,
    header_name: str | None,
    header_value: str | None,
    description: str,
) -> None:
    """Register a global MCP service with an encrypted org credential."""
    credential = header_value
    if auth_type in ("static-header", "bearer") and credential is None:
        credential = click.prompt("Credential", hide_input=True)
    try:
        service, _connection = asyncio.run(
            _with_session(
                lambda session: register_global_mcp(
                    session,
                    slug=slug,
                    name=name,
                    url=url,
                    transport=cast(McpTransport, transport),
                    auth_type=cast(McpAuthType, auth_type),
                    header_name=header_name,
                    credential=credential,
                    description=description,
                )
            )
        )
    except ServiceError as exc:
        raise click.ClickException(str(exc)) from exc
    except IntegrityError:
        raise click.ClickException(f"service slug {slug!r} is already registered") from None
    click.echo(f"Registered global MCP: global/{service.slug}")


@main.command("register-personal-mcp")
@click.option("--email", required=True, help="Email of the owner registering the service.")
@click.option("--slug", required=True, help="Service slug, e.g. team-api.")
@click.option("--name", required=True, help="Display name.")
@click.option("--url", required=True, help="MCP endpoint URL.")
@click.option("--transport", type=TransportChoice, required=True)
@click.option("--auth-type", type=AuthTypeChoice, default="none", show_default=True)
@click.option("--header-name", default=None, help="Header name for static-header auth.")
@click.option(
    "--header-value",
    default=None,
    help="Secret header or bearer value; prompted hidden when required.",
)
@click.option("--description", default="")
def register_personal_mcp_cmd(
    email: str,
    slug: str,
    name: str,
    url: str,
    transport: str,
    auth_type: str,
    header_name: str | None,
    header_value: str | None,
    description: str,
) -> None:
    """Register a personal MCP service under <handle>/<slug>."""
    credential = header_value
    if auth_type in ("static-header", "bearer") and credential is None:
        credential = click.prompt("Credential", hide_input=True)

    async def _register(session: AsyncSession) -> tuple[str, str]:
        owner = await get_user_by_email(session, email)
        if owner is None:
            raise click.ClickException(f"no user with email {email}")
        try:
            service, _connection = await register_personal_mcp(
                session,
                owner=owner,
                slug=slug,
                name=name,
                url=url,
                transport=cast(McpTransport, transport),
                auth_type=cast(McpAuthType, auth_type),
                header_name=header_name,
                credential=credential,
                description=description,
            )
        except ServiceError as exc:
            raise click.ClickException(str(exc)) from exc
        return owner.handle, service.slug

    try:
        handle, resolved_slug = asyncio.run(_with_session(_register))
    except IntegrityError:
        raise click.ClickException(
            f"personal service slug {slug!r} is already registered for this user"
        ) from None
    click.echo(f"Registered personal MCP: {handle}/{resolved_slug}")


@main.command("set-service-status")
@click.option(
    "--service",
    "ref",
    required=True,
    help="Service id: global/<slug> or <handle>/<slug>.",
)
@click.option(
    "--status",
    type=click.Choice(["active", "disabled"]),
    required=True,
)
def set_service_status_cmd(ref: str, status: str) -> None:
    """Enable or disable a global or personal service."""

    async def _apply(session: AsyncSession) -> None:
        try:
            service = await set_service_status(session, ref=ref, status=cast(ServiceStatus, status))
        except ServiceError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Service {ref} is now {service.status}")

    asyncio.run(_with_session(_apply))


@main.command("set-service-visibility")
@click.option("--service", "slug", required=True, help="Service slug, e.g. redash.")
@click.option(
    "--visibility",
    type=click.Choice(["all", "restricted"]),
    required=True,
    help="all: everyone in the org; restricted: only granted users.",
)
def set_service_visibility_cmd(slug: str, visibility: str) -> None:
    """Set a global or template service's visibility."""

    async def _apply(session: AsyncSession) -> None:
        try:
            service = await set_service_visibility(
                session, slug=slug, visibility=cast(ServiceVisibilityLevel, visibility)
            )
        except ServiceError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"global/{service.slug} visibility set to {service.visibility}")

    asyncio.run(_with_session(_apply))


@main.command("grant-service-visibility")
@click.option("--service", "slug", required=True, help="Service slug, e.g. redash.")
@click.option("--user", "user_email", required=True, help="Email of the user to grant access.")
def grant_service_visibility_cmd(slug: str, user_email: str) -> None:
    """Grant a specific user visibility on a restricted service."""

    async def _apply(session: AsyncSession) -> None:
        try:
            service = await grant_service_visibility(session, slug=slug, user_email=user_email)
        except ServiceError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Granted {user_email} access to global/{service.slug}")

    asyncio.run(_with_session(_apply))


@main.command("revoke-service-visibility")
@click.option("--service", "slug", required=True, help="Service slug, e.g. redash.")
@click.option("--user", "user_email", required=True, help="Email of the user to revoke.")
def revoke_service_visibility_cmd(slug: str, user_email: str) -> None:
    """Revoke a user's visibility grant on a service."""

    async def _apply(session: AsyncSession) -> None:
        try:
            service = await revoke_service_visibility(session, slug=slug, user_email=user_email)
        except ServiceError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Revoked {user_email} access to global/{service.slug}")

    asyncio.run(_with_session(_apply))


@main.command("share-service")
@click.option("--service", "ref", required=True, help="Service id: <handle>/<slug>.")
@click.option("--owner", "owner_email", required=True, help="Email of the service owner.")
@click.option("--user", "grantee_email", required=True, help="Email of the user to share with.")
def share_service_cmd(ref: str, owner_email: str, grantee_email: str) -> None:
    """Share an owner's personal service with another user."""

    async def _apply(session: AsyncSession) -> None:
        try:
            share = await share_service(
                session, ref=ref, owner_email=owner_email, grantee_email=grantee_email
            )
        except ShareError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Shared {ref} with {grantee_email} (share {share.id})")

    asyncio.run(_with_session(_apply))


@main.command("unshare-service")
@click.option("--service", "ref", required=True, help="Service id: <handle>/<slug>.")
@click.option("--owner", "owner_email", required=True, help="Email of the service owner.")
@click.option("--user", "grantee_email", required=True, help="Email of the user to unshare.")
def unshare_service_cmd(ref: str, owner_email: str, grantee_email: str) -> None:
    """Revoke a share on an owner's personal service."""

    async def _apply(session: AsyncSession) -> None:
        try:
            share = await unshare_service(
                session, ref=ref, owner_email=owner_email, grantee_email=grantee_email
            )
        except ShareError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Revoked share {share.id} on {ref} for {grantee_email}")

    asyncio.run(_with_session(_apply))


@main.command("deactivate-user")
@click.option("--email", required=True, help="Email of the user to deactivate.")
def deactivate_user_cmd(email: str) -> None:
    """Deactivate a user: revoke tokens, disable connections, close sessions."""

    async def _apply(session: AsyncSession) -> None:
        try:
            user = await deactivate_user(session, email=email)
        except UserError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Deactivated {user.email} (handle {user.handle})")

    asyncio.run(_with_session(_apply))


@main.command("add-url-allowlist")
@click.option(
    "--pattern",
    required=True,
    help="Exact hostname (e.g. windmill.internal) or CIDR (e.g. 10.0.0.0/8).",
)
@click.option("--note", default=None, help="Why this destination is allowed.")
def add_url_allowlist_cmd(pattern: str, note: str | None) -> None:
    """Allow a hostname or CIDR for personal-service outbound requests."""

    async def _apply(session: AsyncSession) -> None:
        try:
            row = await add_url_allowlist(session, pattern=pattern, note=note)
        except AllowlistError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Allowlisted {row.pattern}")

    asyncio.run(_with_session(_apply))


@main.command("remove-url-allowlist")
@click.option("--pattern", required=True, help="Pattern to remove.")
def remove_url_allowlist_cmd(pattern: str) -> None:
    """Remove a hostname or CIDR from the URL allowlist."""

    async def _apply(session: AsyncSession) -> None:
        try:
            normalized = await remove_url_allowlist(session, pattern=pattern)
        except AllowlistError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Removed {normalized} from the allowlist")

    asyncio.run(_with_session(_apply))


if __name__ == "__main__":
    main()
