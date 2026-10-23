"""Automated secret-hygiene checks for the Phase 7 security review.

Greps the source for credential-like values reaching logs or being
hard-coded, and asserts the security-relevant configuration values
documented in docs/security-review.md have not silently drifted.
"""

import ast
import re
from pathlib import Path

import pytest

from cam.auth.lockout import LOCKOUT, MAX_FAILURES_PER_EMAIL, WINDOW
from cam.auth.sessions import SESSION_IDLE_TIMEOUT_S
from cam.config import Settings, generate_encryption_key_entry, parse_encryption_keys
from cam.connections.tokens import (
    TOKEN_PREFIX,
    TOKEN_VISIBLE_PREFIX_LENGTH,
    extract_token_prefix,
    generate_raw_token,
    hash_token,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "cam"

_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "error", "exception", "critical", "log", "log_message"}
)
_SECRET_TERM = (
    r"\b(secret|password|token|credential|api_key|api key|bearer|plaintext|master_key)\w*\b"
)
_TOKEN_LIKE = re.compile(
    r"(cam_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|[A-Za-z0-9+/]{60,}={0,2}"
    r"|-----BEGIN [A-Z ]+PRIVATE KEY-----)"
)


def _module_paths() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _logging_calls(tree: ast.AST) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _LOG_METHODS:
            continue
        base = node.func.value
        name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
        if name == "logging" or "log" in name.lower():
            calls.append(node)
    return calls


def _call_terms(call: ast.Call) -> str:
    terms: list[str] = []
    for arg in (*call.args, *call.keywords):
        for node in ast.walk(arg):
            if isinstance(node, ast.Name):
                terms.append(node.id.lower().replace("_", " "))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                terms.append(node.value.lower())
    return " ".join(terms)


def test_no_credentials_in_logging_calls() -> None:
    offenders: list[str] = []
    for path in _module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _logging_calls(tree):
            if re.search(_SECRET_TERM, _call_terms(call)):
                offenders.append(f"{path.name} line {call.lineno}")
    assert not offenders, f"logging calls mention secret-like terms: {offenders}"


def test_no_credential_literals_in_source() -> None:
    offenders: list[str] = []
    for path in _module_paths():
        text = path.read_text(encoding="utf-8")
        for match in _TOKEN_LIKE.finditer(text):
            literal = match.group(1)
            if literal.startswith("cam_") and literal == TOKEN_PREFIX + "…":
                continue
            offenders.append(f"{path.name}: {literal[:20]}…")
    assert not offenders, f"possible hard-coded credentials in src: {offenders}"


@pytest.mark.parametrize(
    ("attribute", "expected"),
    [
        ("rate_limit_per_min", 120),
        ("default_timeout_s", 60),
        ("default_response_cap_tokens", 50000),
        ("log_retention_days", 30),
    ],
)
def test_documented_setting_defaults(attribute: str, expected: int) -> None:
    assert Settings.model_fields[attribute].default == expected


def test_documented_lockout_values() -> None:
    assert MAX_FAILURES_PER_EMAIL == 5
    assert WINDOW == LOCKOUT
    assert WINDOW.total_seconds() == 15 * 60


def test_documented_session_idle_timeout() -> None:
    assert SESSION_IDLE_TIMEOUT_S == 12 * 60 * 60


def test_token_stores_only_hash_and_short_prefix() -> None:
    raw = generate_raw_token()
    assert raw.startswith(TOKEN_PREFIX)
    prefix = extract_token_prefix(raw)
    assert len(prefix) == TOKEN_VISIBLE_PREFIX_LENGTH
    assert raw.startswith(TOKEN_PREFIX + prefix)
    assert hash_token(raw) != raw
    assert len(hash_token(raw)) == 64


def test_encryption_key_env_format_matches_documented_v1_base64() -> None:
    entry = generate_encryption_key_entry("v1")
    assert re.fullmatch(r"v1:[A-Za-z0-9+/]{43}=", entry)
    keys = parse_encryption_keys(entry)
    assert len(keys["v1"]) == 32
