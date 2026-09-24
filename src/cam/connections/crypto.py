import secrets
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cam.config import Settings, parse_encryption_keys

DATA_KEY_SIZE = 32
_NONCE_SIZE = 12
_GCM_TAG_SIZE = 16
_CREDENTIAL_MAGIC = b"CAMC1"
_WRAP_MAGIC = b"CAMW1"
_CREDENTIAL_HEADER_SIZE = len(_CREDENTIAL_MAGIC) + 1
_WRAP_FIXED_HEADER_SIZE = len(_WRAP_MAGIC) + 3


class EncryptionError(Exception):
    pass


@dataclass(frozen=True)
class DecryptedSecret:
    key_version: int
    plaintext: bytes


class MasterKeyRing:
    def __init__(self, keys: Mapping[str, bytes], current: str) -> None:
        if not keys:
            raise EncryptionError("master key ring is empty")
        for name, key in keys.items():
            if len(key) != DATA_KEY_SIZE:
                raise EncryptionError(f"master key {name!r} must be 32 bytes")
        if current not in keys:
            raise EncryptionError(f"current master key {current!r} is not in the key ring")
        self._keys = dict(keys)
        self._current = current

    @classmethod
    def from_settings(cls, settings: Settings) -> MasterKeyRing:
        return cls(
            parse_encryption_keys(settings.encryption_keys),
            settings.encryption_key_current,
        )

    @property
    def current_name(self) -> str:
        return self._current

    @property
    def current_key(self) -> bytes:
        return self._keys[self._current]

    def get(self, name: str) -> bytes:
        try:
            return self._keys[name]
        except KeyError:
            raise EncryptionError(
                f"master key {name!r} is not available in CAM_ENCRYPTION_KEYS"
            ) from None


_default_ring: MasterKeyRing | None = None


def get_default_key_ring() -> MasterKeyRing:
    global _default_ring
    if _default_ring is None:
        _default_ring = MasterKeyRing.from_settings(Settings())
    return _default_ring


def generate_data_key() -> bytes:
    return secrets.token_bytes(DATA_KEY_SIZE)


def _check_version(version: int) -> None:
    if not 0 <= version <= 255:
        raise EncryptionError("key_version must be between 0 and 255")


def _check_data_key(data_key: bytes) -> None:
    if len(data_key) != DATA_KEY_SIZE:
        raise EncryptionError(f"data key must be {DATA_KEY_SIZE} bytes")


def wrap_data_key(data_key: bytes, ring: MasterKeyRing, *, version: int = 1) -> bytes:
    _check_data_key(data_key)
    _check_version(version)
    name = ring.current_name.encode("ascii")
    header = _WRAP_MAGIC + bytes([version]) + len(name).to_bytes(2, "big") + name
    nonce = secrets.token_bytes(_NONCE_SIZE)
    ciphertext = AESGCM(ring.current_key).encrypt(nonce, data_key, header)
    return header + nonce + ciphertext


def unwrap_data_key(blob: bytes, ring: MasterKeyRing) -> tuple[int, bytes]:
    if len(blob) < _WRAP_FIXED_HEADER_SIZE or blob[: len(_WRAP_MAGIC)] != _WRAP_MAGIC:
        raise EncryptionError("wrapped data key is malformed")
    version = blob[len(_WRAP_MAGIC)]
    name_length = int.from_bytes(blob[len(_WRAP_MAGIC) + 1 : len(_WRAP_MAGIC) + 3], "big")
    header_length = len(_WRAP_MAGIC) + 3 + name_length
    if len(blob) < header_length + _NONCE_SIZE + _GCM_TAG_SIZE:
        raise EncryptionError("wrapped data key is truncated")
    try:
        name = blob[len(_WRAP_MAGIC) + 3 : header_length].decode("ascii")
    except UnicodeDecodeError as exc:
        raise EncryptionError("wrapped data key has an invalid master key name") from exc
    header = blob[:header_length]
    nonce = blob[header_length : header_length + _NONCE_SIZE]
    ciphertext = blob[header_length + _NONCE_SIZE :]
    try:
        data_key = AESGCM(ring.get(name)).decrypt(nonce, ciphertext, header)
    except InvalidTag as exc:
        raise EncryptionError(
            "failed to unwrap data key: wrong master key or corrupted data"
        ) from exc
    _check_data_key(data_key)
    return version, data_key


def rewrap_data_key(blob: bytes, ring: MasterKeyRing) -> bytes:
    version, data_key = unwrap_data_key(blob, ring)
    return wrap_data_key(data_key, ring, version=version)


def encrypt_secret(plaintext: bytes, data_key: bytes, *, key_version: int = 1) -> bytes:
    _check_data_key(data_key)
    _check_version(key_version)
    header = _CREDENTIAL_MAGIC + bytes([key_version])
    nonce = secrets.token_bytes(_NONCE_SIZE)
    ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, header)
    return header + nonce + ciphertext


def decrypt_secret(blob: bytes, data_key: bytes) -> DecryptedSecret:
    _check_data_key(data_key)
    if len(blob) < _CREDENTIAL_HEADER_SIZE + _NONCE_SIZE + _GCM_TAG_SIZE:
        raise EncryptionError("encrypted secret is truncated")
    if blob[: len(_CREDENTIAL_MAGIC)] != _CREDENTIAL_MAGIC:
        raise EncryptionError("encrypted secret is malformed")
    key_version = blob[len(_CREDENTIAL_MAGIC)]
    header = blob[:_CREDENTIAL_HEADER_SIZE]
    nonce = blob[_CREDENTIAL_HEADER_SIZE : _CREDENTIAL_HEADER_SIZE + _NONCE_SIZE]
    ciphertext = blob[_CREDENTIAL_HEADER_SIZE + _NONCE_SIZE :]
    try:
        plaintext = AESGCM(data_key).decrypt(nonce, ciphertext, header)
    except InvalidTag as exc:
        raise EncryptionError("failed to decrypt secret: wrong data key or corrupted data") from exc
    return DecryptedSecret(key_version=key_version, plaintext=plaintext)
