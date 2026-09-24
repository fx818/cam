import base64
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from cam.config import (
    DEV_DATABASE_URL,
    Settings,
    generate_encryption_key_entry,
    parse_encryption_keys,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class CleanSettings(Settings):
    model_config = SettingsConfigDict(
        env_prefix="CAM_",
        env_file=None,
        env_file_encoding="utf-8",
    )


ALL_ENV_VARS = (
    "CAM_ENV",
    "CAM_BASE_URL",
    "CAM_DATABASE_URL",
    "CAM_ENCRYPTION_KEYS",
    "CAM_ENCRYPTION_KEY_CURRENT",
    "CAM_SESSION_SECRET",
    "CAM_DEFAULT_TIMEOUT_S",
    "CAM_DEFAULT_RESPONSE_CAP_TOKENS",
    "CAM_LOG_RETENTION_DAYS",
    "CAM_RATE_LIMIT_PER_MIN",
    "CAM_HEALTHCHECK_INTERVAL_S",
    "CAM_RESYNC_INTERVAL_S",
    "CAM_SESSION_IDLE_TIMEOUT_S",
    "CAM_LOG_LEVEL",
    "CAM_JOBS_ENABLED",
    "CAM_DB_POOL_SIZE",
    "CAM_DB_MAX_OVERFLOW",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ALL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def load() -> Settings:
    return CleanSettings()


def test_dev_defaults(clean_env: None) -> None:
    settings = load()
    assert settings.env == "dev"
    assert settings.database_url == DEV_DATABASE_URL
    assert settings.base_url == "http://localhost:8000"
    assert settings.default_timeout_s == 60
    assert settings.default_response_cap_tokens == 50000
    assert settings.log_retention_days == 30
    assert settings.rate_limit_per_min == 120
    assert settings.healthcheck_interval_s == 300
    assert settings.resync_interval_s == 3600
    assert settings.session_idle_timeout_s == 600
    assert settings.log_level == "INFO"
    keys = parse_encryption_keys(settings.encryption_keys)
    assert settings.encryption_key_current in keys
    assert all(len(key) == 32 for key in keys.values())
    assert len(settings.session_secret) >= 32


def test_dev_ephemeral_keys_differ_between_loads(clean_env: None) -> None:
    first, second = load(), load()
    assert first.encryption_keys != second.encryption_keys
    assert first.session_secret != second.session_secret


def test_env_overrides(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAM_BASE_URL", "https://cam.example.internal")
    monkeypatch.setenv("CAM_DEFAULT_TIMEOUT_S", "30")
    monkeypatch.setenv("CAM_LOG_LEVEL", "debug")
    settings = load()
    assert settings.base_url == "https://cam.example.internal"
    assert settings.default_timeout_s == 30
    assert settings.log_level == "DEBUG"


def test_prod_requires_secrets(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAM_ENV", "prod")
    with pytest.raises(ValidationError, match="CAM_DATABASE_URL") as exc:
        load()
    message = str(exc.value)
    for name in (
        "CAM_DATABASE_URL",
        "CAM_ENCRYPTION_KEYS",
        "CAM_ENCRYPTION_KEY_CURRENT",
        "CAM_SESSION_SECRET",
    ):
        assert name in message


def test_prod_accepts_complete_config(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAM_ENV", "prod")
    monkeypatch.setenv("CAM_DATABASE_URL", "postgresql+asyncpg://cam:cam@db:5432/cam")
    monkeypatch.setenv("CAM_ENCRYPTION_KEYS", generate_encryption_key_entry("v1"))
    monkeypatch.setenv("CAM_ENCRYPTION_KEY_CURRENT", "v1")
    monkeypatch.setenv("CAM_SESSION_SECRET", "prod-session-secret-value")
    settings = load()
    assert settings.env == "prod"
    assert settings.session_secret == "prod-session-secret-value"


def test_invalid_key_base64_rejected(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAM_ENCRYPTION_KEYS", "v1:not-base64!!")
    with pytest.raises(ValidationError, match="not valid base64"):
        load()


def test_key_wrong_length_rejected(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    short = base64.b64encode(b"tooshort").decode()
    monkeypatch.setenv("CAM_ENCRYPTION_KEYS", f"v1:{short}")
    with pytest.raises(ValidationError, match="32 bytes"):
        load()


def test_current_key_must_exist(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAM_ENCRYPTION_KEYS", generate_encryption_key_entry("v1"))
    monkeypatch.setenv("CAM_ENCRYPTION_KEY_CURRENT", "v9")
    with pytest.raises(ValidationError, match="CAM_ENCRYPTION_KEY_CURRENT"):
        load()


def test_bad_log_level_rejected(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAM_LOG_LEVEL", "TRACE")
    with pytest.raises(ValidationError, match="CAM_LOG_LEVEL"):
        load()


def test_non_positive_number_rejected(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAM_DEFAULT_TIMEOUT_S", "0")
    with pytest.raises(ValidationError, match="CAM_DEFAULT_TIMEOUT_S"):
        load()


def test_bad_base_url_rejected(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAM_BASE_URL", "ftp://example")
    with pytest.raises(ValidationError, match="CAM_BASE_URL"):
        load()


def test_env_example_documents_every_var() -> None:
    example = (REPO_ROOT / ".env.example").read_text()
    for var in ALL_ENV_VARS:
        assert var in example


def test_env_is_gitignored() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in gitignore
    assert "!.env.example" in gitignore
