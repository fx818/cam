# CAM — Independent Production-Readiness Review (cross-checked)

**Repo:** `/home/imart/Desktop/Ideas/cam`  ·  **Commit:** `ba38589` (branch `main`)
**Date:** 2026-10-12  ·  **Scope:** full codebase — backend (`src/cam/**`), `tests/**`, `alembic/**`, `deploy/**`, `web/**`, `scripts/**`, CI
**Reviewer 1:** OpenAI Codex CLI 0.162.1, model `gpt-6.1-sol` (read-only sandbox)
**Cross-checker:** OpenCode 1.18.34, model `opencode/big-pickle` (glm-5.3-flash was requested but blocked — Zen/Go account has insufficient funds)
**Independent verification:** Hermes agent (this report) — own probes + test run

## Verdict

**Not production-ready.** The foundations are genuinely good (async SQLAlchemy/httpx, Argon2 off-loop, encrypted credentials, hashed tokens, a single access-resolution module, real-Postgres integration fixtures, strict typing, ruff/pyright clean). But there are exploitable outbound-request paths, a master-key rotation hazard, cross-tenant admin operations, credential-leak vectors, audit loss under routine failure, OAuth correctness defects, and deploy/K8s gaps.

**40 findings:** 3 CRITICAL, 15 HIGH, 18 MEDIUM, 3 LOW, 1 INFO.

Every one of the 40 Codex findings was re-opened against source during cross-check. Result: **37 confirmed TRUE, 2 partly true (mis-cited / sub-claim overstated), 0 false positives.** One candidate issue the cross-checker raised (CSRF blocking bearer callers on mutating `/api`) was investigated and **refuted** (by design; tests assert it). The report is high quality and holds up.

## How this was verified

| Evidence | Result |
|---|---|
| Codex full read of the tree | Report `R01–R40` (raw: `docs/review-artifacts/codex-report.md`) |
| OpenCode cross-check pass 1 | Verified R01–R17, R22, R25, R35 → TRUE (severity nuances) |
| OpenCode cross-check pass 2 | Verified R18–R40 → TRUE; R20 & R40 PARTLY TRUE; CSRF candidate refuted |
| Hermes own probe R01 | `jsonschema` 4.26.0 external `$ref` **did** invoke `urllib.request.urlopen` synchronously (blocked at the socket) |
| Hermes own probe R22 | `session.get(AuditLog, uuid)` **raises** `InvalidRequestError`: PK is `(id, ts)` |
| Hermes pytest (real Postgres) | **569 passed**, 4 warnings, 94s (warning: a cartesian-product `SAWarning` in admin-overview queries) |
| Hermes source reads | R02, R03, R05, R06, R07, R11, R12, R13, R14, R15, R16, R24, R34, R39 all confirmed by directly opening the cited code |

## Verification table (all 40)

| ID | Sev (Codex) | Corrected | Files | Verdict |
|---|---|---|---|---|
| R01 | CRITICAL | CRITICAL | `invoke/validation.py:45`; `registry/openapi.py`; `registry/rest_tools.py` | TRUE (line range off — call is line 45; file is 56 lines) |
| R02 | CRITICAL | HIGH (needs ≥2 orgs; launch is single-org) | `api/routes/admin_users.py`; `accounts/users.py`; `registry/services.py:543` | TRUE |
| R03 | CRITICAL | HIGH (operator-triggered) | `scripts/provision_secrets_gsm.sh:10,32-42` | TRUE |
| R04 | HIGH | HIGH | `api/main.py:174`; `db/session.py`; `audit/writer.py:190` | TRUE |
| R05 | HIGH | HIGH | `upstream/rest.py:222-268` | TRUE |
| R06 | HIGH | HIGH | `audit/writer.py:132-201`; `invoke/pipeline.py:551` | TRUE |
| R07 | HIGH | HIGH | `invoke/pipeline.py:366-373`; `invoke/truncation.py` | TRUE |
| R08 | HIGH | HIGH | `skills/validation.py:262-278`; `api/routes/skills.py` | TRUE |
| R09 | HIGH | HIGH | `connections/oauth.py:483-491`; `db/session.py:26` | TRUE |
| R10 | HIGH | HIGH | `connections/oauth.py:519`; `api/routes/connect.py`; `upstream/pool.py:64` | TRUE |
| R11 | HIGH | HIGH | `registry/resync.py:106` | TRUE |
| R12 | HIGH | HIGH | `api/routes/connect.py:284-302`; `connections/oauth.py` | TRUE |
| R13 | HIGH | HIGH | `alembic/versions/b8d2f1a6c4e7_...py:115-131` | TRUE |
| R14 | HIGH | HIGH | `invoke/pipeline.py:334-365`; `jobs/health.py` | TRUE |
| R15 | HIGH | HIGH | `deploy/gke/hpa.yaml:13-14`; `invoke/rate_limit.py`; `jobs/runner.py` | TRUE |
| R16 | HIGH | HIGH | `auth/lockout.py:40-63` | TRUE |
| R30 | HIGH | HIGH | `deploy/gke/migration-job.yaml`; `deploy/gke/deployment.yaml:84` | TRUE |
| R17 | MEDIUM | MEDIUM | `auth/sessions.py`; `api/deps.py` | TRUE |
| R18 | MEDIUM | MEDIUM | `upstream/client.py:139-155`; `pool.py:66-69` | TRUE |
| R19 | MEDIUM | MEDIUM | `registry/services.py:560-575` | TRUE |
| R20 | MEDIUM | MEDIUM | `api/queries.py:235-245`; `skills/store.py:52-98` | PARTLY TRUE (latter sub-claim overstated) |
| R21 | MEDIUM | MEDIUM | `invoke/pipeline.py:242-246` | TRUE |
| R22 | MEDIUM | MEDIUM | `api/routes/admin_diagnostics.py:95`; `db/models.py:296` | TRUE (probe-confirmed) |
| R23 | MEDIUM | MEDIUM | `api/queries.py:931-952`; `jobs/retention.py:102`; `gateway/server.py:228` | TRUE |
| R24 | MEDIUM | MEDIUM | `jobs/retention.py:86-95` | TRUE |
| R25 | MEDIUM | MEDIUM | `db/models.py`; `db/base.py` | TRUE (partial) |
| R26 | MEDIUM | MEDIUM | `access/resolution.py:101-102,355-413`; `gateway/server.py:150` | TRUE |
| R27 | MEDIUM | MEDIUM | `jobs/health.py:151-174`; `jobs/runner.py:80` | TRUE |
| R28 | MEDIUM | MEDIUM | `api/metrics.py:108-137`; `deploy/grafana/prometheus-alerts.yaml:13` | TRUE |
| R29 | MEDIUM | MEDIUM | `Dockerfile`; `docker-compose.yml:9`; `deploy/gke/*`; `api/main.py:187` | TRUE |
| R31 | MEDIUM | MEDIUM | `deploy/in-cluster-postgres/backup-cronjob.yaml:25`; `scripts/backup_db.sh` | TRUE |
| R32 | MEDIUM | MEDIUM | `web/src/main.tsx`; `web/src/lib/auth.tsx:74-92` | TRUE |
| R33 | MEDIUM | MEDIUM | `.github/workflows/ci.yml`; `tests/conftest.py`; `tests/test_secret_hygiene.py:118` | TRUE |
| R34 | MEDIUM | MEDIUM | `api/queries.py:355-373` | TRUE |
| R35 | MEDIUM | MEDIUM | `upstream/client.py:159-198`; `pool.py:135-154` | TRUE |
| R36 | MEDIUM | MEDIUM | `upstream/rest.py:238-249,288`; `client.py` | TRUE |
| R37 | LOW | LOW | `Dockerfile:1,11`; `pyproject.toml`; `upstream/pinning.py:76-84` | TRUE |
| R38 | LOW | LOW | `scripts/run_cam.ps1:108-111` | TRUE |
| R39 | LOW | LOW | `api/queries.py:343`; `api/routes/services.py:101` | TRUE |
| R40 | INFO | INFO | `docs/architecture.md:707-724` | PARTLY TRUE (security-review.md has only 180 lines; cited 199-239 doesn't exist) |

## CRITICAL findings

### R01 — External JSON-Schema `$ref` bypasses SSRF controls and blocks the event loop
- **Files:** `src/cam/invoke/validation.py:45`; `src/cam/registry/openapi.py`; `src/cam/registry/rest_tools.py`
- **Problem:** `validate_args()` calls `jsonschema_validate(args, schema)` with the library's default registry. jsonschema's default remote-retrieval path calls `urllib.request.urlopen` **synchronously** for a non-local `$ref`. A personal-service tool schema (or an OpenAPI-derived REST schema) can therefore carry an `http(s)` `$ref`.
- **Impact:** An attacker-controlled personal upstream can make CAM issue internal-network requests from inside the request path, outside `upstream/`, bypassing the URL policy, DNS validation and IP pinning — and it blocks the event loop (async rule violation). OpenAPI reference handling also exposes a local-file read surface.
- **Fix:** Supply an explicit `referencing` registry whose retrieve rejects network/file access (only local/allowed refs), and configure OpenAPI validation equivalently; bound schema complexity; move expensive validation off-loop. **Verified by probe (this review).**

### R02 — Administrative operations act on the default org, not the caller's org
- **Files:** `src/cam/api/routes/admin_users.py:63-78`; `src/cam/accounts/users.py:105-126`; `src/cam/api/routes/admin_services.py:112-137`; `src/cam/registry/services.py:543`; `src/cam/connections/orgs.py:18-19`
- **Problem:** Admin user creation, and global/template registration + visibility helpers, call `ensure_org(session)` with the default (IndiaMART) org instead of the authenticated admin's `org_id`. `list_admin_services` *does* filter by `admin.org_id` — the codebase is internally inconsistent.
- **Impact:** In a multi-org deployment an admin of org B can create users/services in IndiaMART's org and read/modify its visibility. A real cross-tenant authorization defect (violates the "never from request input" locked rule).
- **Fix:** Require `org_id` in these functions and derive it only from `admin.org_id`; remove default-org behaviour from HTTP-reachable helpers; add second-org admin tests. *(Cross-check: severity HIGH while the launch is single-org; becomes CRITICAL the moment a second org exists — and "public-readiness" is a stated goal.)*

### R03 — Rerunning secret provisioning destroys the master-key ring
- **Files:** `scripts/provision_secrets_gsm.sh:10,32-42`; `scripts/sync_k8s_secrets.sh`; `deploy/encryption-keys.md:27-29`
- **Problem:** Provisioning **always** mints a fresh `v1:<key>` and `put_secret` adds a new version to the existing GSM secret; sync reads `latest`; the runbook encourages rerunning after rotation. Existing credential envelopes are encrypted under the *old* `v1`, which the new `v1` can no longer decrypt.
- **Impact:** An ordinary re-run makes every stored credential (connection secrets, OAuth tokens, OAuth client secrets) undecryptable; recovery needs the prior GSM version or an offline backup.
- **Fix:** Separate first-time provisioning from rotation: refuse to overwrite an existing ring; rotation appends a *new* key name, retains old keys, and rewraps org data keys transactionally with a decrypt check before retiring the old key. **Verified by reading (this review).**

## HIGH findings

- **R04 — Credentials leak via transport logs (`api/main.py:174`; `db/session.py`; `upstream/rest.py:243`; `audit/writer.py:190`).** Root logging at INFO + installed `httpx`/`httpx2` log full request URLs (including `?token=`/api-key query credentials); SQLAlchemy engine lacks `hide_parameters=True`; raw `str(exc)` in errors. **Fix:** suppress/redact HTTP-client request logging, add a sanitized transport-error formatter, `hide_parameters=True`, redact URLs in exceptions; add a sentinel-credential logging test.
- **R05 — REST redirects forward credentials and replay the operation (`upstream/rest.py:222-268`).** Every hop reuses method, JSON body, query and headers; policy validates the *address* but does not bind credentials to the original origin, and a 301/302/303/307/308 POST is re-POSTed → duplicate side effects. **Fix:** reject credential-bearing cross-origin redirects (or make per-service opt-in), define redirect method/body semantics, pin each hop.
- **R06 — Audit records are discarded on overflow or DB failure (`audit/writer.py:132-201`).** Full queue drops with a warning; `_write` swallows the DB exception and returns normally so `_run` still increments `AUDIT_WRITTEN`; the pipeline ignores `record()`'s result. Executed calls vanish and metrics falsely report success. **Fix:** durable bounded spool/retry, count writes only after commit, expose persistence failures. (Retrying audit persistence is not a retry of the upstream call.)
- **R07 — Structured and error responses bypass the 50k cap (`invoke/pipeline.py:366-373`; `truncation.py`).** Only text blocks are truncated; `outcome.structured` is returned whole; upstream error text is unbounded. **Fix:** cap text + structured JSON + error envelopes under one budget.
- **R08 — Skill ZIP limits applied after decompression (`skills/validation.py:262-278`).** `archive.read(entry)` expands entries before counting/size-checking; sync work in async routes; YAML parsing unbounded. **Fix:** check entry count/declared+total sizes/ratio first, stream with a byte ceiling, run bounded decompression off-loop.
- **R09 — OAuth row lock returns stale ORM state (`connections/oauth.py:483-491`).** The `SELECT … FOR UPDATE` lacks `populate_existing=True`, so the identity-mapped object keeps its old expiry/refresh token; the post-lock re-check reads stale values. Concurrent callers can refresh twice with a rotating refresh token and mark a healthy connection `needs_reconnect`. **Fix:** reload the locked row with `populate_existing=True`, recheck status/expiry/token. **Verified by reading (this review).**
- **R10 — OAuth refresh/reconnect leaves pooled MCP sessions on the old token (`connections/oauth.py:519`; `upstream/pool.py:64`).** MCP headers are fixed at session open; pool keyed only by (service, connection); refresh/connect does not invalidate. CAM decrypts a new token but keeps sending the old one. **Fix:** per-connection credential-generation counter; invalidate sessions on change.
- **R11 — MCP resync skips OAuth credentials (`registry/resync.py:106`).** `build_connection_target` resolves credentials only when `auth_type != "oauth"`; OAuth services are contacted with empty headers → OAuth (e.g. Notion) templates can't import/resync tools. **Fix:** resolve OAuth credentials with the same refresh-aware resolver. **Verified by reading (this review).**
- **R12 — OAuth callbacks can reactivate connections after deactivation/deletion (`api/routes/connect.py:284-302`).** The callback trusts stored state without rechecking the initiating user's active status, service status/soft-delete, or current authorization, then stores tokens and sets `connected`. **Fix:** lock and revalidate state/user/service/org/authorization before exchange; delete pending states on deactivation/deletion. **Verified by reading (this review).**
- **R13 — Partition migration fails on populated databases (`alembic/versions/b8d2f1a6c4e7_…py:115-131`).** `upgrade()` inserts existing rows into the new partitioned table **before** creating any child partitions → "no partition of relation found for row". Fresh-DB migration tests never hit it. **Fix:** create partitions covering existing timestamps before copying; add an upgrade test from the previous revision with data. **Verified by reading (this review).**
- **R14 — A health-write failure turns a successful upstream action into a retryable error (`invoke/pipeline.py:334-365`).** `record_invoke_health` is awaited after dispatch outside any try; a DB error becomes a generic INTERNAL error though the upstream call already succeeded → the agent retries and duplicates the side effect. **Fix:** preserve the dispatch outcome; make health persistence separately observable; distinguish "not dispatched" from "outcome unknown". **Verified by reading (this review).**
- **R15 — Process-local coordination vs multi-replica HPA (`deploy/gke/hpa.yaml:13-14`; `invoke/rate_limit.py`; `jobs/runner.py`; `registry/resync.py`).** Sessions, rate limits, pool invalidation, resync locks and schedulers are all per-process while HPA allows 3 replicas. **Fix:** ship single-replica/one-worker until distributed rate limiting, invalidation and a singleton job runner exist. **Verified by reading (this review).**
- **R16 — Login lockout is evictable and racy (`auth/lockout.py:40-63`).** It selects only the latest 500 failures globally and filters email/IP in Python, so unrelated failures evict relevant ones; check and record are separate unlocked operations. **Fix:** SQL-side targeted counts, atomic admission, and a bounded pre-Argon2 gate. **Verified by reading (this review).**
- **R30 — The K8s migration Job can't use the documented Cloud SQL connection (`deploy/gke/migration-job.yaml`; `deploy/gke/deployment.yaml:84`; `README.md:70`).** The runbook points the DB URL at `127.0.0.1:5432`; only the Deployment carries the Cloud SQL proxy sidecar, not the Job; the app also runs `alembic upgrade head` at every container start (migration races on rollout). **Fix:** give the Job DB transport + controlled proxy lifecycle (or a direct private endpoint); run migrations once pre-rollout and start pods without schema mutation.

## MEDIUM findings

- **R17 — Dashboard sessions aren't revocable (`auth/sessions.py`).** Signed cookie holds only user/org/iat; logout just clears the cookie; password reset doesn't invalidate. Stolen cookies survive. **Fix:** session version/ID, revoke on logout, bump on reset.
- **R18 — Cached MCP session reuses an old URL-policy decision (`upstream/client.py:139-155`; `pool.py:66-69`).** Policy is checked only at session `start()`; a reused pool entry ignores the freshly loaded policy (e.g. a removed allowlist entry). **Fix:** enforce current policy at the connection/request boundary and invalidate on policy change.
- **R19 — Unified registration omits DNS validation (`registry/services.py:560-575`).** The active `register_service` only does static `admit_url`; the older personal-MCP path did `validate_outbound_url` (DNS). **Fix:** DNS-validate personal service/spec URLs at registration.
- **R20 — Access rules duplicated; skill creation leaks service existence (`api/queries.py:235-245`; `skills/store.py:52-98`; `api/routes/skills.py:330-346`).** Template visibility is implemented outside `access/`; skill dependency resolution returns different errors for nonexistent vs inaccessible services. *(Cross-check: the "tool-list filtering" sub-claim is overstated — that's just an enabled/active filter.)* **Fix:** move dashboard access modes + dependency resolution into `access/`; return one opaque result.
- **R21 — Invoke audit omits the credential owner (`invoke/pipeline.py:242-246`).** `AuditRef.owner_user_id` is supported but never set on invoke, so owners can't see others' calls through their credentials. **Fix:** populate it.
- **R22 — "Explain failure" uses an invalid composite-PK lookup (`api/routes/admin_diagnostics.py:95`; `db/models.py:296`).** PK is `(id, ts)`; `db.get(AuditLog, entry_id)` raises `InvalidRequestError` → the endpoint 500s. **Fix:** query by `org_id` + `id`. **Verified by probe (this review).**
- **R23 — Dashboard settings are saved but not applied (`api/queries.py:931-952`; `jobs/retention.py:102`; `gateway/server.py:228`).** Registration/retention read env `Settings`; gateway reads `server_instructions` (while the dashboard writes `instructions`); other orgs get default-org instructions. **Fix:** one typed effective-settings resolver with defined precedence; align setting names.
- **R24 — Monthly partition drops exceed 30-day retention (`jobs/retention.py:86-95`).** A partition drops only when its whole month ended before the cutoff, so boundary-month rows older than 30 days linger. **Fix:** drop fully-expired partitions, then delete expired rows from the boundary partition via an indexed predicate. **Verified by reading (this review).**
- **R25 — Tenant filtering / DB tenant integrity incomplete (`db/models.py`; `db/base.py`).** Some child queries rely on parent UUIDs/post-fetch checks; FKs don't enforce child `org_id` = parent org; `settings` has no `org_id`. **Fix:** explicit org predicates everywhere; composite tenant FKs; reconcile the settings exception against the AGENTS.md rule.
- **R26 — Template identity doesn't match the owner-qualified contract (`access/resolution.py:101-102,355-413`; `gateway/server.py:150`).** Templates are addressed `global/<slug>` (spec says owner-qualified); resolution silently prefers own-then-first-shared; the per-user description excludes templates; no `tools/list_changed` for access changes (architecture.md:439 requires it). **Fix:** owner-qualified resolution + discovery + notifications, or get owner sign-off on the changed contract.
- **R27 — Health jobs unbounded; per-connection health collapses (`jobs/health.py:151-174`; `jobs/runner.py:80`).** Unbounded `asyncio.gather`; multiple template outcomes overwrite one `Service.health`; `scheduler.shutdown(wait=False)`. **Fix:** bound concurrency, record per-connection health, aggregate deterministically, await job shutdown.
- **R28 — Monitoring misses tool failures; misleading alerts (`api/metrics.py:108-137`; `deploy/grafana/prometheus-alerts.yaml:13`).** 5xx alert misses MCP tool errors returned as HTTP 200; unmatched paths become raw metric labels; 30-min stall threshold vs hourly/daily jobs; health counts probe outcomes not services. **Fix:** instrument tool outcomes at the pipeline, fixed unmatched-route label, per-job heartbeats.
- **R29 — Deployment hardening/health semantics incomplete (`Dockerfile`; `docker-compose.yml:9`; `deploy/gke/*`; `api/main.py:187`).** No non-root/securityContext for cam; compose publishes Postgres on all interfaces with `cam/cam`; liveness hits a DB-dependent endpoint; `enable_dns_rebinding_protection=False` in prod too; ingress exposes unauthenticated `/api/metrics`. **Fix:** non-root image + security context, loopback-bind compose DB, split liveness/readiness, enable host/origin protection, restrict metrics.
- **R31 — Backup scripts unproven for CAM recovery (`deploy/in-cluster-postgres/backup-cronjob.yaml:25`; `scripts/backup_db.sh`/`restore_db.sh`/`backup_restore_test.sh`).** CronJob passes DB name `/cam`; docker branches omit `-U`; restore interpolates the DB name into SQL; the non-docker test branch ignores its target; the test round-trips only a synthetic table. **Fix:** fix args/quoting, repair non-docker path, prove a restored CAM DB + offline keys through login/decrypt.
- **R32 — Frontend caches survive logout/user changes (`web/src/main.tsx`; `web/src/lib/auth.tsx:74-92`; `web/src/pages/ActivityLogPage.tsx`).** One persistent QueryClient; logout/login never cancel/clear; audit query keys omit user/org with `keepPreviousData`. **Fix:** cancel+clear on logout/identity change; namespace sensitive keys.
- **R33 — CI/tests don't enforce the readiness claims (`.github/workflows/ci.yml`; `tests/conftest.py:41-60`; `tests/test_secret_hygiene.py:118`).** CI is Python-only (no frontend build/tests, no coverage gate); DB fixture drops/recreates (fresh migrations only); the hygiene test is lexical and line 118 ends `... or True` (tautology). **Fix:** add frontend checks + coverage gates + populated-upgrade & concurrency regressions; guard destructive test URLs.
- **R34 — Service deletion leaves OAuth client secrets (`api/queries.py:355-373`).** Soft-delete wipes connection secrets but leaves `service.auth_secret_enc` and outstanding OAuth states. **Fix:** clear service secrets and revoke pending states in the same transaction. **Verified by reading (this review).**
- **R35 — Pool startup/retries/status classification concurrency defects (`upstream/client.py:159-198`; `pool.py:135-154`).** Startup retries internally *and* the pool retries again (exceeding "one reconnect"); cancellation while waiting for `_ready` may leave the owner task/transport; a shared `self._statuses` list is cleared/drained across concurrent calls. **Fix:** own retry policy in one layer, clean up owner on cancellation, correlate statuses per request.
- **R36 — Request timeout and response-memory bounds incomplete (`upstream/rest.py:238-249,288`; `client.py`).** `httpx.Timeout` is per-phase, not end-to-end (redirects/slow bodies exceed the budget); full bodies buffered before truncation. **Fix:** explicit dispatch deadline across DNS→establish→redirect→read; stream with hard byte limits; bound discovery/schema sizes.

## LOW / INFO

- **R37 (LOW) — Reproducibility gaps (`Dockerfile:1,11`; `pyproject.toml`; `upstream/pinning.py:76-84`).** Mutable image tags/action SHAs; broad dep lower bounds; pinning pokes private `transport._pool._network_backend` on both HTTP stacks. **Fix:** pin digests/SHAs, declare imported transport deps explicitly, gate transport-internals changes with regression tests.
- **R38 (LOW) — Windows launcher drops bootstrap env (`scripts/run_cam.ps1:108-111`).** Host env vars aren't passed into `docker compose exec` (no `-e`), so `create-admin` sees empty vars; `install` copies the script so `$PSScriptRoot` points outside the repo. **Fix:** pass `-e` explicitly; persist the install repo path; test on Windows.
- **R39 (LOW) — Avoidable 500s on valid input (`api/queries.py:343`; `api/routes/services.py:101`).** `model_dump(exclude_unset=True)` keeps explicit nulls; `int(updates["timeout_ms"])` raises `TypeError` (only `ValueError` is caught) → 500; cursor `fromisoformat`/`UUID` parse errors uncaught. **Fix:** reject null on non-nullable fields; translate cursor/conversion errors to 422. **Verified by reading (this review).**
- **R40 (INFO) — Release gates incomplete (`docs/architecture.md:707-724`).** Security sign-off (7.1), pilot, TLS deploy, backup/restore confirmation, secret-manager deploy, rollout all unchecked; security-review.md holds questions, not approval. *(Cross-check: the report's cited `security-review.md:199-239` doesn't exist — the file is 180 lines.)*

## Cross-check corrections

1. **R02 severity** — CRITICAL in the original; the exploit needs ≥2 orgs and the launch is single-org → HIGH today, CRITICAL the moment multi-org is used.
2. **R03 severity** — CRITICAL in the original; operator-triggered (not request-path) → HIGH, still a data-loss hazard.
3. **R20** — "tool-list filtering implements access rules" is overstated; that path is only an enabled/active filter. The skill-existence-oracle part stands.
4. **R40** — invalid citation `docs/security-review.md:199-239` (file is 180 lines); the substance (gates unchecked) is correct.
5. **R01 line range** — cited `validation.py:47–65`; the file is 56 lines and the actual call is line 45 (the generic `except Exception` at ~54 swallows the error but the network fetch still happens).
6. **Candidate gap (refuted):** the cross-checker suspected `CsrfMiddleware` wrongly blocks bearer-token callers of mutating `/api`. Investigated: tests add `X-CSRF-Token` to bearer requests (`tests/api/helpers.py:90-92`), bearer+CSRF is used together (`tests/gateway/test_phase3_done_when.py:106`), and the spec ties CSRF to dashboard/mutating `/api` while agents use `/mcp` — **by design, not a defect.**

## What could NOT be verified

- Live GKE behavior (ingress affinity, TLS, proxy-header trust, IAM, secret access, NetworkPolicy) and actual backup/PITR recovery.
- Real Notion OAuth compatibility; live upstreams; Claude Code/Cursor behavior.
- Security-team sign-off, pilot usage, offline key custody, alert delivery.
- Dependency CVE status (no vulnerability DB / provenance audit run).
- Windows execution of the launcher (findings are static).
- A full git-history secret scan (only the tracked inventory was checked; `.env` is gitignored).

## Top blockers (fix before any production rollout)

1. **R01** — uncontrolled schema `$ref` retrieval (SSRF + sync network in the request path).
2. **R03** — provisioning re-run invalidates every stored credential.
3. **R05 + R04** — credential forwarding on redirects and credential leakage into transport logs.
4. **R02** — administrative operations crossing tenant boundaries.
5. **R13 + R30** — populated-database migration failure and a non-functional K8s migration Job.
6. **R06 + R14** — audit loss under routine failure and successful-upstream-turned-error (duplicate side effects).

## Suggested remediation order

1. **Week 1 (security/data-integrity):** R01, R03, R04, R05, R13, R14, R22.  *R22 is a ~0.5-day fix (query by `org_id`+`id`).*
2. **Week 2 (auth/lifecycle):** R02, R09, R10, R11, R12, R16, R17, R34.
3. **Week 3 (correctness/scale):** R06, R07, R08, R15, R18, R19, R24, R30, R35, R36.
4. **Week 4 (ops/hardening):** R20, R21, R23, R25, R26, R27, R28, R29, R31, R32, R33, R37, R38, R39.
5. **Before release:** complete R40 gates — security sign-off on the corrected code, pilot, deploy + recovery evidence.

## Owner decisions needed

- **Multi-org:** is a second org going live? If yes, R02/R25 are CRITICAL now. If no, note it as a launch blocker for the multi-tenant milestone.
- **Template addressing (R26):** implement owner-qualified IDs + `tools/list_changed`, or get explicit sign-off on the current `global/<slug>` contract.
- **Scaling (R15):** ship single-replica (documented) until distributed state exists, or fund the distributed work now.
- **Audit durability (R06):** acceptable to lose audit rows under outage, or is a durable spool required for the 30-day guarantee?
