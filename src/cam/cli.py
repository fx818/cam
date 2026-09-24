import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

import click
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cam import __version__
from cam.accounts.users import UserError, UserKind, create_user
from cam.config import Settings
from cam.connections.orgs import DEFAULT_ORG_SLUG
from cam.db.session import create_db_engine, create_session_factory

UserKindChoice = click.Choice(["person", "team"])


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
    resolved_password = _resolve_password(password)
    try:
        user = asyncio.run(
            _with_session(
                lambda session: create_user(
                    session,
                    email=email,
                    password=resolved_password,
                    role="admin",
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


if __name__ == "__main__":
    main()
