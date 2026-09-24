import base64
import binascii
import secrets
from typing import Literal, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_DATABASE_URL = "postgresql+asyncpg://cam:cam@localhost:5432/cam"
LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
PRODUCTION_REQUIRED_FIELDS = (
    "database_url",
    "encryption_keys",
    "encryption_key_current",
    "session_secret",
)


def generate_encryption_key_entry(name: str = "v1") -> str:
    return f"{name}:{base64.b64encode(secrets.token_bytes(32)).decode('ascii')}"


def parse_encryption_keys(raw: str) -> dict[str, bytes]:
    keys: dict[str, bytes] = {}
    for part in raw.split(","):
        entry = part.strip()
        if not entry or ":" not in entry:
            raise ValueError("encryption keys must be name:<base64> entries separated by commas")
        name, _, encoded = entry.partition(":")
        if not name:
            raise ValueError("encryption key name must not be empty")
        try:
            key = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"encryption key {name!r} is not valid base64") from exc
        if len(key) != 32:
            raise ValueError(f"encryption key {name!r} must decode to 32 bytes")
        if name in keys:
            raise ValueError(f"duplicate encryption key name {name!r}")
        keys[name] = key
    if not keys:
        raise ValueError("at least one encryption key is required")
    return keys


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CAM_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    env: Literal["dev", "prod"] = "dev"
    base_url: str = "http://localhost:8000"
    database_url: str = ""
    encryption_keys: str = ""
    encryption_key_current: str = ""
    session_secret: str = ""
    default_timeout_s: int = 60
    default_response_cap_tokens: int = 50000
    log_retention_days: int = 30
    rate_limit_per_min: int = 120
    healthcheck_interval_s: int = 300
    resync_interval_s: int = 3600
    session_idle_timeout_s: int = 600
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _finalize(self) -> Self:
        if self.env == "prod":
            missing = [f for f in PRODUCTION_REQUIRED_FIELDS if not getattr(self, f)]
            if missing:
                names = ", ".join(f"CAM_{name.upper()}" for name in missing)
                raise ValueError(f"required when CAM_ENV=prod: {names}")
        else:
            if not self.database_url:
                self.database_url = DEV_DATABASE_URL
            if not self.encryption_keys:
                self.encryption_keys = generate_encryption_key_entry("v1")
            if not self.session_secret:
                self.session_secret = secrets.token_urlsafe(32)
        keys = parse_encryption_keys(self.encryption_keys)
        if not self.encryption_key_current:
            if self.env == "prod":
                raise ValueError("required when CAM_ENV=prod: CAM_ENCRYPTION_KEY_CURRENT")
            self.encryption_key_current = next(iter(keys))
        if self.encryption_key_current not in keys:
            raise ValueError("CAM_ENCRYPTION_KEY_CURRENT must name a key in CAM_ENCRYPTION_KEYS")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("CAM_BASE_URL must be an http(s) URL")
        level = self.log_level.upper()
        if level not in LOG_LEVELS:
            raise ValueError(f"CAM_LOG_LEVEL must be one of {sorted(LOG_LEVELS)}")
        self.log_level = level
        for field in (
            "default_timeout_s",
            "default_response_cap_tokens",
            "log_retention_days",
            "rate_limit_per_min",
            "healthcheck_interval_s",
            "resync_interval_s",
            "session_idle_timeout_s",
        ):
            if getattr(self, field) < 1:
                raise ValueError(f"CAM_{field.upper()} must be >= 1")
        return self
