# PROGRESS.md

## 2026-09-24 — Phase 0.1: repo and tooling

- `uv` project (`pyproject.toml`, `uv.lock`, Python 3.14 pinned), hatchling build with `src/cam` package.
- ruff (lint + format), pyright (strict), pytest + pytest-asyncio (auto mode) configured in `pyproject.toml`.
- `.pre-commit-config.yaml`: ruff check/format, standard hygiene hooks, pyright, detect-private-key, and a local guard that blocks committing `.env`.
- `.gitignore` excludes `.env` (keeps `.env.example`), `.venv`, caches, `node_modules`.
- Smoke test passes; `ruff check`, `ruff format`, `pyright`, `pytest` all green.

Notable: pre-commit hooks installed into `.git/hooks`.

## 2026-09-24 — Phase 0.2: local stack

- `docker-compose.yml`: Postgres 16 (healthcheck `pg_isready`, init script creates `cam_test`) + CAM app built from `Dockerfile` (uv image, uvicorn), app healthcheck hits the DB-backed health endpoint.
- FastAPI app under `src/cam/api/`: `create_app()` factory with lifespan that creates/disposes an async SQLAlchemy engine; thin `GET /api/health` route that runs `SELECT 1` and returns 200/503.
- `src/cam/db/session.py`: engine factory + async `ping()`; placeholder packages created for `gateway/`, `upstream/`, `access/`, `connections/`, `registry/`, `skills/`, `audit/`, `jobs/`, plus `web/` and `tests/fixtures/` placeholders.
- `.dockerignore` keeps `.git`, `.venv`, caches out of the build context.
- Tests: health 200 with real Postgres, 503 when the DB is unreachable (no DB mocks). pytest loop scopes set to session so async fixtures and tests share one loop.

Notable: `docker compose up --build -d` → both services healthy; `curl localhost:8000/api/health` → `{"status":"ok"}` 200.

## Open questions

_None._
