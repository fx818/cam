from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from cam.accounts.users import UserError, get_user_by_email
from cam.connections.passwords import verify_password
from cam.db.models import User

DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHRzb21lc2FsdA$"
    "IZ6hK+1Lsm51ZqKBmT0nJqG0OJ5nPvQGvX0Nfs2gC1E"
)


class AuthProvider(Protocol):
    name: str

    async def authenticate(
        self, session: AsyncSession, email: str, password: str
    ) -> User | None: ...


class EmailPasswordProvider:
    name = "email-password"

    async def authenticate(self, session: AsyncSession, email: str, password: str) -> User | None:
        try:
            user = await get_user_by_email(session, email)
        except UserError:
            await verify_password(password, DUMMY_PASSWORD_HASH)
            return None
        if user is None:
            await verify_password(password, DUMMY_PASSWORD_HASH)
            return None
        if not await verify_password(password, user.password_hash):
            return None
        if user.status != "active" or user.deactivated_at is not None:
            return None
        return user


def default_auth_provider() -> AuthProvider:
    return EmailPasswordProvider()
