import hashlib
import secrets
import string
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cam.db.models import CamToken, User

TOKEN_PREFIX = "cam_"
TOKEN_BODY_LENGTH = 43
TOKEN_VISIBLE_PREFIX_LENGTH = 8
_RAW_TOKEN_BYTES = 32
BASE62_ALPHABET = string.digits + string.ascii_letters


def _encode_base62(data: bytes) -> str:
    value = int.from_bytes(data, "big")
    digits: list[str] = []
    while value:
        value, remainder = divmod(value, 62)
        digits.append(BASE62_ALPHABET[remainder])
    encoded = "".join(reversed(digits)) or BASE62_ALPHABET[0]
    return encoded.rjust(TOKEN_BODY_LENGTH, BASE62_ALPHABET[0])


def generate_raw_token() -> str:
    return TOKEN_PREFIX + _encode_base62(secrets.token_bytes(_RAW_TOKEN_BYTES))


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def extract_token_prefix(raw_token: str) -> str:
    body = raw_token.removeprefix(TOKEN_PREFIX)
    return body[:TOKEN_VISIBLE_PREFIX_LENGTH]


@dataclass(frozen=True)
class AuthenticatedIdentity:
    org_id: uuid.UUID
    user: User
    token: CamToken


async def create_token(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    label: str,
) -> tuple[str, CamToken]:
    if not label.strip():
        raise ValueError("token label must not be empty")
    raw_token = generate_raw_token()
    token = CamToken(
        org_id=org_id,
        user_id=user_id,
        label=label,
        token_hash=hash_token(raw_token),
        token_prefix=extract_token_prefix(raw_token),
    )
    session.add(token)
    await session.flush()
    return raw_token, token


async def lookup_token(session: AsyncSession, raw_token: str) -> CamToken | None:
    if not raw_token.startswith(TOKEN_PREFIX):
        return None
    result = await session.execute(
        select(CamToken).where(CamToken.token_hash == hash_token(raw_token))
    )
    return result.scalar_one_or_none()


async def authenticate_token(session: AsyncSession, raw_token: str) -> AuthenticatedIdentity | None:
    if not raw_token.startswith(TOKEN_PREFIX):
        return None
    stmt = (
        select(CamToken, User)
        .join(User, CamToken.user_id == User.id)
        .where(
            CamToken.token_hash == hash_token(raw_token),
            CamToken.revoked_at.is_(None),
            User.status == "active",
            User.deactivated_at.is_(None),
        )
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    token, user = row
    return AuthenticatedIdentity(org_id=token.org_id, user=user, token=token)


async def mark_token_used(session: AsyncSession, token: CamToken) -> None:
    token.last_used_at = datetime.now(UTC)
    await session.flush()
