# PROGRESS.md

## 2026-09-24 — Phase 0.1: repo and tooling

- `uv` project (`pyproject.toml`, `uv.lock`, Python 3.14 pinned), hatchling build with `src/cam` package.
- ruff (lint + format), pyright (strict), pytest + pytest-asyncio (auto mode) configured in `pyproject.toml`.
- `.pre-commit-config.yaml`: ruff check/format, standard hygiene hooks, pyright, detect-private-key, and a local guard that blocks committing `.env`.
- `.gitignore` excludes `.env` (keeps `.env.example`), `.venv`, caches, `node_modules`.
- Smoke test passes; `ruff check`, `ruff format`, `pyright`, `pytest` all green.

Notable: pre-commit hooks installed into `.git/hooks`.

## Open questions

_None._
