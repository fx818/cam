import base64
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cam.config import parse_encryption_keys
from cam.connections.crypto import (
    EncryptionError,
    MasterKeyRing,
    decrypt_secret,
    encrypt_secret,
    generate_data_key,
    get_default_key_ring,
    rewrap_data_key,
    unwrap_data_key,
    wrap_data_key,
)
from cam.connections.orgs import (
    OrgNotFoundError,
    decrypt_for_org,
    encrypt_for_org,
    ensure_org,
    get_org_data_key,
)
from cam.connections.passwords import hash_password, verify_password
from cam.connections.tokens import (
    BASE62_ALPHABET,
    TOKEN_PREFIX,
    authenticate_token,
    create_token,
    extract_token_prefix,
    generate_raw_token,
    hash_token,
    lookup_token,
    mark_token_used,
)
from cam.db.models import Org, User


def _ring(names: tuple[str, ...] = ("v1",), current: str = "v1") -> MasterKeyRing:
    keys = {name: bytes([index + 1]) * 32 for index, name in enumerate(names)}
    return MasterKeyRing(keys, current)


def _unique_handle(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def test_hash_and_verify_password() -> None:
    password = "correct horse battery staple"
    password_hash = await hash_password(password)
    assert password_hash.startswith("$argon2id$")
    assert password_hash != password
    assert await verify_password(password, password_hash) is True
    assert await verify_password("wrong password", password_hash) is False


async def test_verify_rejects_malformed_hash() -> None:
    assert await verify_password("anything", "not-a-real-hash") is False


def test_generate_raw_token_format() -> None:
    raw = generate_raw_token()
    assert raw.startswith(TOKEN_PREFIX)
    body = raw.removeprefix(TOKEN_PREFIX)
    assert len(body) == 43
    assert all(char in BASE62_ALPHABET for char in body)
    assert len(extract_token_prefix(raw)) == 8
    assert extract_token_prefix(raw) == body[:8]


def test_generate_raw_token_unique() -> None:
    tokens = {generate_raw_token() for _ in range(100)}
    assert len(tokens) == 100


def test_hash_token_is_sha256_hex() -> None:
    raw = generate_raw_token()
    digest = hash_token(raw)
    assert len(digest) == 64
    assert all(char in "0123456789abcdef" for char in digest)
    assert digest != raw
    assert digest == hash_token(raw)


async def test_create_and_authenticate_token(db_session: AsyncSession) -> None:
    org = await ensure_org(db_session)
    handle = _unique_handle("tokenuser")
    user = User(
        org_id=org.id,
        email=f"{handle}@example.com",
        handle=handle,
        display_name="Token User",
        password_hash="x",
        role="user",
        kind="person",
    )
    db_session.add(user)
    await db_session.flush()
    raw, token = await create_token(db_session, org_id=org.id, user_id=user.id, label="laptop")
    await db_session.commit()
    assert token.token_hash == hash_token(raw)
    assert token.token_hash != raw
    assert token.token_prefix == extract_token_prefix(raw)
    found = await lookup_token(db_session, raw)
    assert found is not None
    assert found.id == token.id
    identity = await authenticate_token(db_session, raw)
    assert identity is not None
    assert identity.user.id == user.id
    assert identity.org_id == org.id
    assert identity.token.id == token.id


async def test_unknown_and_malformed_tokens_rejected(db_session: AsyncSession) -> None:
    assert await lookup_token(db_session, "cam_unknownvalue") is None
    assert await authenticate_token(db_session, "cam_unknownvalue") is None
    assert await lookup_token(db_session, "not-a-cam-token") is None
    assert await authenticate_token(db_session, "not-a-cam-token") is None


async def test_revoked_token_rejected(db_session: AsyncSession) -> None:
    org = await ensure_org(db_session)
    handle = _unique_handle("revokeduser")
    user = User(
        org_id=org.id,
        email=f"{handle}@example.com",
        handle=handle,
        display_name="Revoked",
        password_hash="x",
        role="user",
        kind="person",
    )
    db_session.add(user)
    await db_session.flush()
    raw, token = await create_token(db_session, org_id=org.id, user_id=user.id, label="revoked")
    token.revoked_at = datetime.now(UTC)
    await db_session.commit()
    assert await lookup_token(db_session, raw) is not None
    assert await authenticate_token(db_session, raw) is None


async def test_deactivated_user_token_rejected(db_session: AsyncSession) -> None:
    org = await ensure_org(db_session)
    handle = _unique_handle("inactiveuser")
    user = User(
        org_id=org.id,
        email=f"{handle}@example.com",
        handle=handle,
        display_name="Inactive",
        password_hash="x",
        role="user",
        kind="person",
        status="deactivated",
        deactivated_at=datetime.now(UTC),
    )
    db_session.add(user)
    await db_session.flush()
    raw, _ = await create_token(db_session, org_id=org.id, user_id=user.id, label="inactive")
    await db_session.commit()
    assert await lookup_token(db_session, raw) is not None
    assert await authenticate_token(db_session, raw) is None


async def test_mark_token_used(db_session: AsyncSession) -> None:
    org = await ensure_org(db_session)
    handle = _unique_handle("touchuser")
    user = User(
        org_id=org.id,
        email=f"{handle}@example.com",
        handle=handle,
        display_name="Touch",
        password_hash="x",
        role="user",
        kind="person",
    )
    db_session.add(user)
    await db_session.flush()
    raw, token = await create_token(db_session, org_id=org.id, user_id=user.id, label="touch")
    await db_session.commit()
    assert token.last_used_at is None
    looked_up = await lookup_token(db_session, raw)
    assert looked_up is not None
    await mark_token_used(db_session, looked_up)
    await db_session.commit()
    refreshed = await lookup_token(db_session, raw)
    assert refreshed is not None
    assert refreshed.last_used_at is not None


async def test_create_token_rejects_blank_label(db_session: AsyncSession) -> None:
    org = await ensure_org(db_session)
    handle = _unique_handle("blanklabel")
    user = User(
        org_id=org.id,
        email=f"{handle}@example.com",
        handle=handle,
        display_name="Blank",
        password_hash="x",
        role="user",
        kind="person",
    )
    db_session.add(user)
    await db_session.flush()
    with pytest.raises(ValueError, match="label"):
        await create_token(db_session, org_id=org.id, user_id=user.id, label="   ")


def test_wrap_unwrap_roundtrip() -> None:
    ring = _ring()
    data_key = generate_data_key()
    blob = wrap_data_key(data_key, ring, version=1)
    assert blob != data_key
    version, unwrapped = unwrap_data_key(blob, ring)
    assert version == 1
    assert unwrapped == data_key


def test_unwrap_works_after_current_key_changes() -> None:
    old_ring = _ring(("v1", "v2"), current="v1")
    data_key = generate_data_key()
    blob = wrap_data_key(data_key, old_ring, version=1)
    new_ring = _ring(("v1", "v2"), current="v2")
    version, unwrapped = unwrap_data_key(blob, new_ring)
    assert version == 1
    assert unwrapped == data_key


def test_rewrap_uses_new_master_key() -> None:
    old_ring = _ring(("v1", "v2"), current="v1")
    data_key = generate_data_key()
    blob = wrap_data_key(data_key, old_ring, version=7)
    new_ring = _ring(("v1", "v2"), current="v2")
    rewrapped = rewrap_data_key(blob, new_ring)
    version, unwrapped = unwrap_data_key(rewrapped, new_ring)
    assert version == 7
    assert unwrapped == data_key


def test_unwrap_missing_master_key_fails() -> None:
    ring = _ring(("v1",))
    blob = wrap_data_key(generate_data_key(), ring, version=1)
    other = _ring(("v2",), current="v2")
    with pytest.raises(EncryptionError, match="not available"):
        unwrap_data_key(blob, other)


def test_unwrap_wrong_master_key_fails() -> None:
    blob = wrap_data_key(generate_data_key(), _ring(("v1",)), version=1)
    wrong = MasterKeyRing({"v1": b"\xff" * 32}, "v1")
    with pytest.raises(EncryptionError, match="failed to unwrap"):
        unwrap_data_key(blob, wrong)


def test_encrypt_decrypt_roundtrip_with_key_version() -> None:
    data_key = generate_data_key()
    blob = encrypt_secret(b"super-secret", data_key, key_version=3)
    assert b"super-secret" not in blob
    result = decrypt_secret(blob, data_key)
    assert result.key_version == 3
    assert result.plaintext == b"super-secret"


def test_decrypt_with_wrong_data_key_fails() -> None:
    blob = encrypt_secret(b"payload", generate_data_key(), key_version=1)
    with pytest.raises(EncryptionError, match="failed to decrypt"):
        decrypt_secret(blob, generate_data_key())


def test_decrypt_tampered_blob_fails() -> None:
    data_key = generate_data_key()
    blob = bytearray(encrypt_secret(b"payload", data_key, key_version=1))
    blob[-1] ^= 0xFF
    with pytest.raises(EncryptionError, match="failed to decrypt"):
        decrypt_secret(bytes(blob), data_key)


def test_decrypt_malformed_blob_fails() -> None:
    with pytest.raises(EncryptionError):
        decrypt_secret(b"short", generate_data_key())
    with pytest.raises(EncryptionError):
        decrypt_secret(b"NOPE1" + b"\x00" * 40, generate_data_key())


def test_default_key_ring_is_stable_per_process() -> None:
    assert get_default_key_ring() is get_default_key_ring()


async def test_ensure_org_provisions_stable_data_key(db_session: AsyncSession) -> None:
    slug = f"org-key-{uuid.uuid4().hex[:10]}"
    org = await ensure_org(db_session, slug=slug, name="Key Org")
    await db_session.commit()
    assert org.data_key_enc is not None
    first = org.data_key_enc
    version, data_key = await get_org_data_key(db_session, org.id)
    assert version == 1
    assert len(data_key) == 32
    again = await ensure_org(db_session, slug=slug, name="Key Org")
    assert again.id == org.id
    assert again.data_key_enc == first


async def test_ensure_default_indiamart_org_with_data_key(db_session: AsyncSession) -> None:
    org = await ensure_org(db_session)
    await db_session.commit()
    assert org.slug == "indiamart"
    assert org.data_key_enc is not None


async def test_encrypt_and_decrypt_for_org_roundtrip(db_session: AsyncSession) -> None:
    slug = f"org-enc-{uuid.uuid4().hex[:10]}"
    org = await ensure_org(db_session, slug=slug, name="Enc Org")
    await db_session.flush()
    blob, key_version = await encrypt_for_org(db_session, org.id, "header-value-123")
    await db_session.commit()
    assert key_version == 1
    assert b"header-value-123" not in blob
    result = await decrypt_for_org(db_session, org.id, blob)
    assert result.key_version == key_version
    assert result.plaintext == b"header-value-123"


async def test_decrypt_for_org_with_other_orgs_key_fails(db_session: AsyncSession) -> None:
    org_a = await ensure_org(db_session, slug=f"org-a-{uuid.uuid4().hex[:10]}", name="A")
    org_b = await ensure_org(db_session, slug=f"org-b-{uuid.uuid4().hex[:10]}", name="B")
    await db_session.flush()
    blob, _ = await encrypt_for_org(db_session, org_a.id, "secret")
    _, key_b = await get_org_data_key(db_session, org_b.id)
    with pytest.raises(EncryptionError, match="failed to decrypt"):
        decrypt_secret(blob, key_b)
    result = await decrypt_for_org(db_session, org_a.id, blob)
    assert result.plaintext == b"secret"
    assert org_a.data_key_enc is not None


async def test_org_operations_missing_org(db_session: AsyncSession) -> None:
    missing = uuid.uuid4()
    with pytest.raises(OrgNotFoundError):
        await get_org_data_key(db_session, missing)
    with pytest.raises(OrgNotFoundError):
        await encrypt_for_org(db_session, missing, b"x")


async def test_default_org_slug_unique(db_session: AsyncSession) -> None:
    duplicate = Org(slug="indiamart", name="Duplicate")
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


def test_master_key_ring_validates_inputs() -> None:
    with pytest.raises(EncryptionError, match="32 bytes"):
        MasterKeyRing({"v1": b"short"}, "v1")
    with pytest.raises(EncryptionError, match="not in the key ring"):
        MasterKeyRing({"v1": b"\x01" * 32}, "v9")
    with pytest.raises(EncryptionError, match="empty"):
        MasterKeyRing({}, "v1")


def test_data_key_wrap_format_roundtrips_base64_key_entry() -> None:
    raw_entry = base64.b64encode(bytes(range(32))).decode()
    keys = parse_encryption_keys(f"v1:{raw_entry}")
    ring = MasterKeyRing(keys, "v1")
    data_key = generate_data_key()
    blob = wrap_data_key(data_key, ring)
    assert unwrap_data_key(blob, ring)[1] == data_key
