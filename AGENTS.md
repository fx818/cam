# AGENTS.md — building CAM (Central Agent Management)

Copy this file to the root of the CAM repo as `AGENTS.md` (and as `CLAUDE.md`, or a symlink to it, for Claude Code). Copy `central-agent-management-cam-architecture-and-build-plan.md` into the repo as `docs/architecture.md`.

## What you are building

CAM is an internal IndiaMART hub. Agents (Claude Code, Codex, Cursor, custom agents) connect to one MCP tool, `cam`. Through it they list, describe and invoke tools on upstream MCP servers and REST APIs, and load text skills. CAM holds the credentials, enforces who can reach what, and logs every call.

`docs/architecture.md` is the source of truth. Read all of it before writing any code. Where sections disagree, the **Implementation spec** wins over earlier sections.

## How to work

1. **Build in phase order**, following the Build plan in `docs/architecture.md`. Phases 0 → 1 → 2 are strictly sequential. Don't start a phase until the previous phase's "Done when" passes.
2. **Work one sub-phase at a time.** For each one:
    - write the code, plus tests covering it,
    - run lint (`ruff`), type check (`pyright`) and tests (`pytest`); all must pass,
    - tick its checkbox in `docs/architecture.md`,
    - add a short entry to `PROGRESS.md`: date, sub-phase, what was built, anything notable.
3. **Make each phase's "Done when" an automated test** before you call the phase complete. Also list the manual check (e.g. "connect Claude Code and invoke a tool") in `PROGRESS.md` for the owner to confirm.
4. **Commit per sub-phase** with a clear message, e.g. `phase-1.4: upstream MCP client and session pool`.

## Locked decisions — do not change

The **Decision log** in `docs/architecture.md` lists decisions the owner has already made. Do not change, "improve" or work around any of them without the owner's explicit approval. In particular:

- **Stack:** Python + FastAPI, the official `mcp` Python SDK, Postgres (not Supabase), React + Vite + TypeScript over a JSON API (not server-rendered templates).
- **Pass-through model:** CAM never adds or narrows tool permissions and has no approval steps.
- **Accounts:** admin-created, email login, no SSO, admin-only password changes.
- **Invoke behaviour:** never retry an upstream call, 60s default timeout, 50,000-token response cap, full args in audit logs, 30-day retention.
- **Skills:** text only. Reject script files at upload.
- **Launch scope:** no Gmail template (Notion first); MCP tools only, with text, JSON and images (no resources, prompts, sampling or elicitation).

## Non-negotiable engineering rules

- **Async everywhere in the request path.** Use asyncpg / async SQLAlchemy and httpx. Never use `requests`, sync DB drivers, `time.sleep`, or other blocking calls in code reachable from `/mcp` or `/api`. Run unavoidable blocking work (e.g. argon2) in a thread pool.
- **Access control is the security boundary.**
    - Every path that lists, describes or invokes goes through the single access-resolution module (`access/`).
    - No shortcuts; don't duplicate access checks elsewhere.
    - Unauthorized requests return `NOT_FOUND`, never a message that reveals the service exists.
- **Secrets:**
    - Never log credentials or tokens, and never return them from the API.
    - Never commit keys or `.env` files. Provide `.env.example` instead.
    - Credentials are injected only after the audit args are captured.
- **URL policy:** every outbound request for a personal service goes through the URL policy checker, at request time as well as at registration. Pin the connection to the validated IP. Don't follow redirects without validating each hop.
- **Public-readiness (see that section of the plan):**
    - Every table has `org_id`. Every query filters by the caller's org, taken from their token or session, never from request input.
    - Login goes through the `AuthProvider` interface.
    - Credentials are encrypted with the org's data key.
    - No IndiaMART-specific values in code: instructions, branding and examples come from `orgs.settings`.
    - Only `upstream/` opens connections to services.
- **Business logic in service modules.** FastAPI routes and the MCP gateway stay thin.
- **Migrations only via Alembic.** Never edit the schema by hand.
- **Tests use real Postgres** (the docker-compose test DB) plus the mock MCP, REST and OAuth servers described under Testing. Don't mock the database in integration tests.

## Repo layout

Follow the layout in the Tech stack section: `gateway/`, `upstream/`, `access/`, `connections/`, `registry/`, `skills/`, `audit/`, `api/`, `jobs/`, `web/`, `db/`. Keep test fixtures (the mock servers) under `tests/fixtures/`.

## When to stop and ask the owner

Stop and ask, instead of deciding yourself, when:

- The spec is silent or ambiguous on something that changes behaviour visible to users, agents or admins.
- A locked decision seems to block progress or looks wrong.
- A library or SDK doesn't support something the spec requires, e.g. an MCP SDK limitation on the client or server side.
- You need real credentials or access, e.g. Windmill or Grafana tokens for Phase 1, or a Notion OAuth setup for Phase 3.
- A security trade-off comes up that the spec doesn't cover.

Write the question in `PROGRESS.md` under **Open questions**, with your recommended answer, then wait.

For purely internal choices that don't affect behaviour (helper names, file splits, internal data structures), decide yourself and keep going.

## Definition of done (whole product)

All nine phases have their boxes ticked and their "Done when" tests passing. The security review (7.1) is signed off. CAM runs on the server (Phase 8).
