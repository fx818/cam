# CAM production-readiness review

**Verdict: No — the repository is not production-ready.**

The implementation has useful foundations: async SQLAlchemy, threaded Argon2 operations, encrypted credentials, hashed agent tokens, access-resolution modules, real Postgres integration fixtures, and strict typing. However, there are exploitable outbound-request paths, cross-org administrative operations, credential leakage risks, audit loss, OAuth correctness defects, and deployment failures.

## Review scope and verification

Reviewed the project instructions and specification first, then inspected backend modules, gateway and upstream transports, database models and migrations, tests and mock servers, frontend authentication and pages, deployment manifests, scripts, configuration, lockfiles, and CI.

The review covers the current working tree, including pre-existing uncommitted launcher scripts. HEAD was `ba38589`. The working tree already contained a modified `PROGRESS.md` and untracked `rough.txt`, `scripts/run_cam.sh`, and `scripts/run_cam.ps1`. These remained unchanged.

Checks performed:

| Check | Result |
|---|---|
| Ruff lint, with caching disabled | Passed |
| Ruff format check, with caching disabled | Passed; 197 files already formatted |
| Strict Pyright | Passed; zero errors or warnings |
| Network-disabled JSON Schema reference probe | Confirmed an attempted fetch to `http://169.254.169.254/review-schema`; no network request was sent |
| Pytest | Not run: its autouse fixture forcibly drops and recreates the configured database (`tests/conftest.py:41–60`) |
| Frontend build/tests | Not run; build and tooling may write artifacts |
| Deployment, live upstreams, backups | Inspected statically; not executed |

**Confidence terminology:** “verified” means established from repository code or installed dependency code, sometimes with a harmless local probe. It does not imply exploitation against a deployed system. “Inferred” identifies production consequences or environment-dependent behavior.

## Findings

### R01 — External schema references bypass SSRF controls and block the event loop

**Severity: CRITICAL**

**Locations:** `src/cam/invoke/validation.py:47–65`; `src/cam/registry/openapi.py:67–85,289–307`; `src/cam/registry/rest_tools.py:100–115`.

**Problem:** `jsonschema_validate(args, schema)` uses the library’s default reference registry. The installed library retrieves external `$ref` URLs through synchronous `urllib.request.urlopen`. Manual REST schemas can retain external references. OpenAPI validation also uses dependency-provided HTTP/file reference handlers without CAM’s URL policy.

A network-disabled probe confirmed that validation of an external reference attempted to fetch a metadata IP.

**Production impact:** A personal-service owner or malicious upstream schema can cause internal-network requests outside `upstream/`, bypass DNS validation, IP pinning, and redirect controls, and stall all requests on the worker. OpenAPI reference resolution additionally exposes a local-file-reading surface.

**Fix:** Reject non-local references before accepting schemas, or pre-resolve approved references asynchronously through `upstream/`. Supply an explicit `referencing.Registry` whose retrieval function rejects network/file access. Configure OpenAPI validation with equivalent restrictions. Bound schema complexity and move expensive validation off the event loop.

**Confidence:** High; verified by code, dependency inspection, and a network-disabled probe.

### R02 — Administrative operations act on the default org instead of the caller’s org

**Severity: CRITICAL**

**Locations:** `src/cam/api/routes/admin_users.py:63–78`; `src/cam/accounts/users.py:105–126`; `src/cam/api/routes/admin_services.py:112–137,153–170,238–309`; `src/cam/registry/services.py:308–327,535–551`; `src/cam/connections/orgs.py:18–19`.

**Problem:** Admin user creation does not pass the authenticated admin’s org. Global/template registration and visibility helpers call `ensure_org()` with the default IndiaMART org. Visibility reads, writes, grants, revocations, and admin resync therefore select that org even when the caller belongs elsewhere.

**Production impact:** An admin in another org can create accounts/services in IndiaMART and inspect or modify IndiaMART service visibility. This is an actual cross-tenant authorization defect, not merely missing query predicates.

**Fix:** Make `org_id` mandatory in these service functions and derive it exclusively from `admin.org_id`. Remove default-org behavior from HTTP-reachable administrative helpers. Add second-org admin tests for every affected endpoint.

**Confidence:** High; verified by reading the call chains.

### R03 — Rerunning secret provisioning replaces the existing master-key ring

**Severity: CRITICAL**

**Locations:** `scripts/provision_secrets_gsm.sh:10–12,32–42`; `scripts/sync_k8s_secrets.sh:8–10,18–24`; `deploy/encryption-keys.md:27–29,53–70`.

**Problem:** Provisioning always generates a fresh `v1` key and publishes it as a new version of existing secrets. Synchronization reads `latest` and restarts CAM. The runbook encourages rerunning these scripts after rotation, but provisioning neither preserves the old ring nor rewraps existing org data keys.

**Production impact:** Existing credential envelopes become unreadable after an otherwise ordinary provisioning rerun. Recovery requires restoring the previous GSM version or an offline backup.

**Fix:** Separate first-time provisioning from rotation. Refuse to overwrite an existing ring during provisioning. Rotation must append a new named key, retain old keys, transactionally rewrap org data keys, verify decryption, and only then retire the old key.

**Confidence:** High; verified from scripts and runbook.

### R04 — HTTP client logging exposes query credentials

**Severity: HIGH**

**Locations:** `src/cam/api/main.py:174`; `src/cam/upstream/url.py:23–31`; `src/cam/invoke/pipeline.py:391–397`; `src/cam/upstream/rest.py:243–253`; `src/cam/upstream/pool.py:146–155`; `src/cam/audit/writer.py:190–201`.

**Problem:** Root logging runs at INFO. Installed `httpx` and `httpx2` log full request URLs at INFO, including credential query parameters. REST transport exceptions are also converted with unredacted `str(exc)`. Debug tracebacks and audit-write exception traces introduce further opportunities to expose URLs or full argument payloads.

The source-only hygiene test cannot detect dependency logging (`tests/test_secret_hygiene.py:39–75`).

**Production impact:** API keys enter container logs, centralized logging, and potentially API error responses. SQLAlchemy exception traces can put full audited arguments into operational logs.

**Fix:** Suppress or redact HTTP-client request logging; use a central sanitized transport-error formatter; configure SQLAlchemy with `hide_parameters=True`; avoid unrestricted exception text in external errors. Test emitted logs using sentinel credentials for both HTTP stacks.

**Confidence:** High; verified through repository and installed HTTP-client code.

### R05 — REST redirects forward credentials and repeat the original operation

**Severity: HIGH**

**Locations:** `src/cam/upstream/rest.py:222–268`.

**Problem:** Every redirect hop reuses the original method, JSON body, query parameters, and headers. URL policy validates whether a destination address is allowed, but does not bind credentials to the original origin.

**Production impact:** A redirect to another public host receives the stored credential and request body. A POST returning 301/302/303 can also cause another POST, creating duplicate side effects.

**Fix:** Reject credential-bearing cross-origin redirects unless explicitly configured for that service. Define redirect method/body semantics explicitly; do not blindly replay mutating operations. Validate and pin every permitted hop.

**Confidence:** High; verified from the request loop.

### R06 — Audit records are permanently discarded on queue overflow or database failure

**Severity: HIGH**

**Locations:** `src/cam/audit/writer.py:145–156,174–201`; `src/cam/invoke/pipeline.py:562–563`.

**Problem:** A full queue drops events. Database failures are swallowed, after which queued events are marked complete. The pipeline ignores `record()`’s failure result. Failed writes also increment “written” metrics because `_write()` returns normally.

**Production impact:** Executed calls disappear from the audit trail during outages or load spikes. Monitoring can report successful audit writes for records that never reached Postgres.

**Fix:** Introduce a durable, bounded spool or transactional audit mechanism. Retain failed batches for retry—retrying audit persistence does not retry upstream calls. Count writes only after commit, expose persistence failures, and define explicit behavior when durable storage is exhausted.

**Confidence:** High; verified. Existing tests intentionally accept dropping behavior (`tests/audit/test_writer.py:97,126`).

### R07 — Structured responses and error responses bypass the response cap

**Severity: HIGH**

**Locations:** `src/cam/invoke/pipeline.py:342–369`; `src/cam/upstream/normalize.py:68–81`; `src/cam/upstream/rest.py:163–187`; `src/cam/invoke/truncation.py:45–103`.

**Problem:** Only text content blocks are truncated. `outcome.structured` is returned unchanged. Upstream error text is converted into an error result before truncation. Objects containing top-level arrays also fall back to text cutting rather than preserving whole items.

**Production impact:** A nominally truncated response can still return the entire JSON object, exceed the 50,000-token contract, exhaust agent context, and inflate memory/network usage. Error responses are similarly unbounded.

**Fix:** Normalize and cap text plus structured JSON under one budget. Return matching truncated structured content. Apply caps to error envelopes as well, and implement the specified object-with-array behavior.

**Confidence:** High; verified.

### R08 — Skill ZIP limits are applied after decompression

**Severity: HIGH**

**Locations:** `src/cam/skills/validation.py:262–278`; `src/cam/api/routes/skills.py:252–266,328–330,476`.

**Problem:** `archive.read(entry)` decompresses every file before checking file count or expanded sizes. This synchronous work runs directly inside async routes. YAML parsing is also performed there and has no alias/depth complexity bound.

**Production impact:** An authenticated user can upload a small compressed bomb that consumes substantial memory and CPU, blocking the worker before validation rejects it.

**Fix:** Check entry count, declared expanded sizes, total size, and compression ratio before reading. Stream each entry with a hard byte ceiling. Run bounded decompression/parsing in a thread pool and limit YAML complexity.

**Confidence:** High; verified.

### R09 — OAuth row locking does not refresh the ORM’s stale connection state

**Severity: HIGH**

**Locations:** `src/cam/connections/oauth.py:475–520`; `src/cam/db/session.py:27–28`.

**Problem:** Callers load `Connection` before entering `refresh_if_needed`. The later `SELECT … FOR UPDATE` lacks `populate_existing=True`, so an already-loaded SQLAlchemy identity may retain its old expiry and refresh token after another caller commits a refresh.

**Production impact:** Concurrent callers can refresh twice with the same rotating refresh token. The second failure can mark a valid connection `needs_reconnect`.

**Fix:** Reload the locked row using `execution_options(populate_existing=True)` or an explicit locked refresh, including `org_id`. Recheck status, expiry, and token from the freshly loaded state. Test two independent sessions with a single-use refresh token.

**Confidence:** High; verified implementation defect; concurrent runtime outcome inferred.

### R10 — OAuth refresh and reconnect leave pooled MCP credentials stale

**Severity: HIGH**

**Locations:** `src/cam/connections/oauth.py:519–526`; `src/cam/api/routes/connect.py:214–246,291–304`; `src/cam/upstream/pool.py:64–79`; `src/cam/upstream/client.py:319–335`.

**Problem:** MCP headers are fixed when the session opens. Pool reuse considers only service/connection IDs. OAuth refresh and connect updates do not invalidate that session; reconnect also leaves the old transport alive.

**Production impact:** CAM decrypts a new token but continues sending the old token, causing repeated authentication failures until the session expires or the process restarts.

**Fix:** Give connections a credential-generation counter. Require pool entries to match it and invalidate on refresh/reconnect. Coordinate generation changes across replicas.

**Confidence:** High; verified.

### R11 — MCP resync deliberately skips OAuth credentials

**Severity: HIGH**

**Locations:** `src/cam/registry/resync.py:99–129,133–148`.

**Problem:** `build_connection_target` resolves credentials only when `service.auth_type != "oauth"`. OAuth services are contacted with empty headers.

**Production impact:** A normal OAuth-protected Notion template cannot import or resync tools. The first-connect import enqueue does not repair this path.

**Fix:** Resolve credentials for OAuth connections too, using the same refresh-aware material resolver. Handle reconnect failures explicitly and test OAuth-authenticated MCP discovery end to end.

**Confidence:** High; verified.

### R12 — OAuth callbacks can reactivate connections after deactivation or deletion

**Severity: HIGH**

**Locations:** `src/cam/api/routes/connect.py:269–304`; `src/cam/connections/oauth.py:398–433`; `src/cam/connections/oauth.py:364`.

**Problem:** Callback processing trusts stored state but does not recheck the initiating user’s active status, the service’s status, or current management/connect authorization. Token storage sets the connection back to `connected`.

**Production impact:** A pending authorization can complete after its owner is deactivated or its service is deleted, recreating credentials and undoing lifecycle enforcement. Template shared-connection resolution does not independently check the owner’s status (`src/cam/access/resolution.py:398–410`).

**Fix:** Lock and revalidate state, user, service, org, and authorization before exchange/storage. Delete outstanding OAuth states on deactivation/deletion, and verify active connection owners during access resolution.

**Confidence:** High; verified missing checks; race outcome inferred.

### R13 — Partition migration fails when existing audit rows are present

**Severity: HIGH**

**Locations:** `alembic/versions/b8d2f1a6c4e7_audit_logs_partitions.py:115–132`.

**Problem:** The migration creates an empty partitioned parent and copies existing rows into it before creating any child partitions.

**Production impact:** Upgrading a populated pre-partition database fails with “no partition … found for row.” Fresh-database migration tests do not expose this.

**Fix:** Create destination partitions covering all existing timestamps before copying. Validate counts, then swap tables. Add an upgrade test starting from the previous revision with historical audit data.

**Confidence:** High; verified by migration order.

### R14 — A health-write failure can turn a successful upstream action into an error

**Severity: HIGH**

**Locations:** `src/cam/invoke/pipeline.py:330–365`; `src/cam/jobs/health.py:197–221`.

**Problem:** After the upstream operation completes, the pipeline awaits a separate database health update without isolating failures. A database exception becomes a generic, retryable INTERNAL result.

**Production impact:** A write may have succeeded upstream, but the agent receives a retryable error and repeats it, duplicating the side effect.

**Fix:** Preserve the upstream outcome once dispatch completes. Treat health persistence as separately observable metadata work. Report ambiguous execution state distinctly where appropriate, and test database failure after a successful mutating call.

**Confidence:** High; verified call ordering; duplicate effect inferred.

### R15 — Multi-replica deployment is incompatible with process-local coordination

**Severity: HIGH**

**Locations:** `deploy/gke/hpa.yaml:13–14`; `deploy/gke/service.yaml:16–19`; `src/cam/api/main.py:145–168,196–198`; `src/cam/registry/resync.py:76–80`; `src/cam/invoke/rate_limit.py:34–59`.

**Problem:** Sessions, rate limits, pool invalidation, resync locks, and schedulers are process-local, while HPA permits three replicas. ClientIP affinity behind an ingress does not provide dependable per-agent session routing and does not coordinate jobs or invalidation.

**Production impact:** Session requests can reach the wrong pod, rate limits multiply, rotation leaves old credentials on other pods, and concurrent jobs race on the same database.

**Fix:** Initially deploy one replica and one worker. Before scaling, implement reliable session routing, distributed rate limiting/invalidation, and a singleton or distributed-lock job runner. Test actual ingress routing and rolling updates.

**Confidence:** High for process-local state; routing behavior environment-dependent and inferred.

### R16 — Login lockout is evictable and not concurrency-safe

**Severity: HIGH**

**Locations:** `src/cam/auth/lockout.py:15,45–58`; `src/cam/api/routes/auth.py:39–48`.

**Problem:** Lockout examines only the latest 500 failures across the entire system, then filters email/IP in Python. Unrelated failures can push relevant attempts out of consideration. Check and recording are separate, unlocked operations.

**Production impact:** Distributed login traffic can bypass account lockout. Concurrent requests can all enter expensive Argon2 verification before the limit takes effect.

**Fix:** Count attempts with SQL predicates for the target email/IP, use atomic admission or locking, and enforce bounded login concurrency before Argon2. Add distributed-flood and concurrent-attempt tests.

**Confidence:** High; verified.

### R17 — Dashboard sessions cannot be revoked on logout or password reset

**Severity: MEDIUM**

**Locations:** `src/cam/auth/sessions.py:40–48,51–82`; `src/cam/api/routes/auth.py:69–72`; `src/cam/api/routes/admin_users.py:144–158`; `src/cam/api/deps.py:98–112`.

**Problem:** Sessions contain signed user/org/time values but no server-side revocation ID or user session version. Logout only deletes the browser cookie, and password reset leaves existing cookies valid.

**Production impact:** A stolen cookie survives logout/password reset and can be refreshed repeatedly while the account remains active.

**Fix:** Include and validate a session version or persisted session ID. Revoke the current session on logout and increment the user’s session version on password reset.

**Confidence:** High; verified.

### R18 — Personal MCP requests reuse an old URL-policy decision

**Severity: MEDIUM**

**Locations:** `src/cam/upstream/client.py:139–155,234–249`; `src/cam/upstream/pool.py:64–79`; `src/cam/invoke/pipeline.py:295–299,405`.

**Problem:** Destination validation happens during session startup. A reused pool entry ignores the newly loaded `UrlPolicy`, including allowlist changes, and does not revalidate each outbound request.

**Production impact:** Removing an allowlist exemption does not stop an existing session from reaching the destination. Subsequent transport connections continue using the old admission decision.

**Fix:** Enforce current policy at the HTTP request/connection boundary and invalidate entries when policy changes. Preserve IP pinning and avoid resolving a different address between validation and connection.

**Confidence:** High; verified. Pinning helps against ordinary DNS rebinding, but does not satisfy request-time policy enforcement.

### R19 — The active registration function omits DNS validation

**Severity: MEDIUM**

**Locations:** `src/cam/registry/services.py:560–575`; compare `src/cam/registry/services.py:207–215`.

**Problem:** The older personal-MCP registration function performs `validate_outbound_url`; the unified `register_service` used by APIs performs only static `admit_url`.

**Production impact:** A hostname resolving to a blocked private IP can be saved despite the required registration-time DNS check. Subsequent outbound checks may prevent the call, but registration claims and behavior are incorrect.

**Fix:** DNS-validate personal service and spec URLs in the unified registration function before persistence.

**Confidence:** High; verified.

### R20 — Access rules are duplicated, and skill creation leaks service existence

**Severity: MEDIUM**

**Locations:** `src/cam/api/queries.py:235–245,376–383`; `src/cam/skills/store.py:52–98`; `src/cam/api/routes/skills.py:330–346`.

**Problem:** Dashboard template visibility and tool-list filtering implement access rules outside `access/`. Skill dependency resolution looks up services without caller access and returns different errors for nonexistent versus existing-but-inaccessible services.

**Production impact:** Access behavior can drift between API and MCP. Skill creation provides an existence oracle for restricted services, violating opaque NOT_FOUND behavior.

**Fix:** Move dashboard access modes and dependency resolution into `access/`, accepting caller identity. Return the same opaque result for absent and inaccessible dependencies.

**Confidence:** High; verified.

### R21 — Invoke audit records omit the credential owner

**Severity: MEDIUM**

**Locations:** `src/cam/invoke/pipeline.py:242–246`; `src/cam/audit/writer.py:57–68`; `src/cam/api/queries.py:673–680,811`.

**Problem:** The invoke `AuditRef` includes service, connection, and tool but leaves `owner_user_id` unset.

**Production impact:** Owners cannot see other users’ calls through their credentials in “my services” activity. Owner attribution is especially important for template shares.

**Fix:** Capture `connection.user_id` or the appropriate service owner in the reference. Test owner-visible activity and audit detail after a shared invocation.

**Confidence:** High; verified.

### R22 — “Explain failure” uses an invalid composite primary-key lookup

**Severity: MEDIUM**

**Locations:** `src/cam/api/routes/admin_diagnostics.py:95`; `src/cam/db/models.py:295–308`.

**Problem:** `AuditLog` has primary key `(id, ts)`, but `db.get(AuditLog, entry_id)` supplies only one component.

**Production impact:** The explain endpoint fails before producing diagnostics.

**Fix:** Use an explicit query filtered by `AuditLog.org_id == admin.org_id` and `AuditLog.id == entry_id`, or carry the timestamp in the API contract.

**Confidence:** High; verified.

### R23 — Dashboard settings are saved but not applied to runtime behavior

**Severity: MEDIUM**

**Locations:** `src/cam/api/queries.py:931–952`; `src/cam/registry/services.py:609–628`; `src/cam/jobs/retention.py:102–109`; `src/cam/gateway/server.py:228–242`.

**Problem:** Admin settings update `orgs.settings`, but registration and retention use environment `Settings`. The dashboard writes `instructions`; gateway startup reads `server_instructions` from the default org.

**Production impact:** Admins believe they changed timeouts, caps, retention, or routing instructions when actual behavior remains unchanged. Other orgs receive default-org instructions.

**Fix:** Introduce a typed effective-settings resolver with defined precedence. Use it consistently for new services, retention, and per-org initialization instructions. Keep setting names consistent.

**Confidence:** High; verified.

### R24 — Monthly partition drops retain logs beyond 30 days

**Severity: MEDIUM**

**Locations:** `src/cam/jobs/retention.py:86–110`; `tests/jobs/test_retention.py:83–113`.

**Problem:** A partition is dropped only when its entire month ends before the cutoff. Rows older than 30 days remain in the boundary month.

**Production impact:** Full arguments can remain for approximately another month, violating the configured retention expectation.

**Fix:** Drop fully expired partitions, then delete expired rows from the boundary partition using an indexed timestamp predicate. Respect effective org retention settings.

**Confidence:** High; verified. Tests use two-month-old records and miss the boundary case.

### R25 — Tenant filtering and database tenant integrity are incomplete

**Severity: MEDIUM**

**Locations:** `src/cam/access/resolution.py:339–344,363–365`; `src/cam/api/queries.py:600–620,806–808`; `src/cam/skills/store.py:231–268`; `src/cam/db/models.py:74–76,181–183,281–286`; `src/cam/db/base.py:11–15`.

**Problem:** Numerous child-table queries rely on parent UUIDs or post-fetch checks instead of explicit org predicates. Foreign keys do not enforce that child `org_id` matches the parent’s org. `settings` has no `org_id`.

**Production impact:** Tenant isolation depends on application consistency; malformed cross-org rows can defeat that assumption. This also violates the explicit “every query filters by caller org” rule.

**Fix:** Add org predicates to all tenant service functions and composite tenant foreign keys where appropriate. Authentication bootstrap lookups should be explicitly isolated exceptions. Reconcile the global `settings` exception: `PROGRESS.md` records it, but AGENTS.md remains stricter.

**Confidence:** High; verified gaps. No additional normal-data cross-org exploit is claimed beyond R02.

### R26 — Template connection identity does not match the specified owner-qualified contract

**Severity: MEDIUM**

**Locations:** `src/cam/access/resolution.py:112–121,205–219,387–413`; `src/cam/gateway/server.py:149–151`; `src/cam/gateway/server.py:245–254`.

**Problem:** Template tools are addressed as `global/<slug>/<tool>`, rather than owner-qualified IDs. Resolution silently prefers the caller’s connection and otherwise selects the first shared connection. The per-user description also calls `list_services` without templates, and there is no gateway session notification mechanism for access changes.

**Production impact:** Agents cannot reliably select whose template credential performs an action. Connected templates can be absent from the tool description, and open sessions retain stale discovery guidance.

**Fix:** Implement explicit owner-qualified template resolution, stable sharing attribution, template discovery, and `tools/list_changed` notifications—or obtain and document owner approval for a changed contract.

**Confidence:** High; verified implementation/spec mismatch.

### R27 — Health jobs are unbounded and collapse per-user connection health

**Severity: MEDIUM**

**Locations:** `src/cam/jobs/health.py:71–115,158–187`; `src/cam/jobs/resync.py:17–45`; `src/cam/jobs/runner.py:80–86`.

**Problem:** Health checks open a probe for every connection concurrently with unbounded `gather`. Multiple template outcomes overwrite one service-level health value, so the last result wins. Shutdown calls `scheduler.shutdown(wait=False)`.

**Production impact:** Growing connection counts can exhaust sockets and upstream quotas. One healthy connection can conceal another expired connection. Shutdown can interrupt jobs while database resources are being disposed.

**Fix:** Bound probe concurrency, record connection health separately, aggregate service health deterministically, and explicitly await/cancel tracked jobs before database shutdown.

**Confidence:** High; verified; exhaustion severity depends on scale.

### R28 — Monitoring misses tool failures and contains misleading alerts

**Severity: MEDIUM**

**Locations:** `src/cam/api/metrics.py:108–113,131–137`; `src/cam/api/main.py:98–108`; `deploy/grafana/prometheus-alerts.yaml:13–18,47–52`; `src/cam/jobs/health.py:164–170`.

**Problem:** HTTP 5xx alerts miss MCP tool errors returned with HTTP 200. Unmatched paths become arbitrary metric labels. The scheduler stalled threshold is 30 minutes for jobs scheduled hourly or daily. Health counts count connection probe outcomes rather than distinct services.

**Production impact:** Real invoke failures go unnoticed, ordinary scheduler intervals trigger false alerts, and unauthenticated path traffic can grow metric cardinality.

**Fix:** Add bounded action/error/latency metrics at the invoke pipeline, use a fixed unmatched-route label, set per-job heartbeat deadlines, and count distinct services or label connection metrics accurately.

**Confidence:** High; verified.

### R29 — Deployment hardening and health semantics are incomplete

**Severity: MEDIUM**

**Locations:** `Dockerfile:11–34`; `docker-compose.yml:4–10,26–33`; `deploy/gke/deployment.yaml:26–92`; `src/cam/api/routes/health.py:10–14`; `src/cam/api/main.py:187`; `deploy/gke/ingress.yaml:21–31`.

**Problem:** CAM runs without a non-root user/security context. Compose publishes Postgres on all interfaces with `cam/cam`. Kubernetes liveness uses a DB-dependent health endpoint. Inbound MCP rebinding protection is disabled in production as well as development. The ingress exposes the unauthenticated metrics route.

**Production impact:** Container compromise has unnecessary privileges; local deployment can expose the database; DB outages cause restart storms; host/origin protection and monitoring isolation are absent from supplied deployment defaults.

**Fix:** Use a non-root image and restrictive security context; bind local DB ports to loopback; separate liveness from readiness; configure production host/origin protection; restrict metrics to monitoring traffic.

**Confidence:** High for configuration; exposure depends on the deployed network.

### R30 — The Kubernetes migration job cannot use the documented Cloud SQL connection

**Severity: HIGH**

**Locations:** `deploy/gke/migration-job.yaml:20–62`; `deploy/gke/deployment.yaml:93–118`; `deploy/gke/README.md:65–85`; `Dockerfile:34`.

**Problem:** The runbook sets the database URL to `127.0.0.1:5432`. Only the Deployment contains the Cloud SQL proxy; the migration Job does not. The app additionally runs Alembic on every container startup.

**Production impact:** The documented migration Job cannot connect. Startup migrations can race during rolling updates or HPA scaling.

**Fix:** Give the migration Job a working database transport and controlled proxy lifecycle, or use a direct private database endpoint. Run migrations once before rollout and start app pods without schema mutation.

**Confidence:** High; verified documented configuration.

### R31 — Backup scripts are not validated as a recoverable CAM backup procedure

**Severity: MEDIUM**

**Locations:** `deploy/in-cluster-postgres/backup-cronjob.yaml:25–26`; `scripts/backup_db.sh:14–20`; `scripts/restore_db.sh:16–18`; `scripts/backup_restore_test.sh:17–24,34–50`; `deploy/backup-restore.md:11–19`.

**Problem:** The CronJob passes `/cam` as the database name. Docker backup/restore commands omit explicit `-U` handling. Restore interpolates the database name into SQL. The non-Docker test branch ignores its target database, while dump/restore functions still require Docker. The recovery test proves only a synthetic table round trip.

**Production impact:** Backups may fail, target the wrong database, or fail to demonstrate recovery of CAM’s partitioned audit schema and encrypted credentials.

**Fix:** Correct the database argument, consistently pass connection parameters, safely quote identifiers, repair the non-Docker path, and test a restored CAM database plus offline keys through login and credential decryption.

**Confidence:** High; verified scripts. Actual backup infrastructure was not inspected.

### R32 — Frontend caches survive logout and user changes

**Severity: MEDIUM**

**Locations:** `web/src/main.tsx:10–17`; `web/src/lib/auth.tsx:74–92`; `web/src/pages/ActivityLogPage.tsx:104–114`.

**Problem:** The application uses one persistent QueryClient. Logout and login update auth state but never cancel or clear cached data. Query keys omit user/org identity; audit queries retain previous data.

**Production impact:** Another user signing in through the same browser can briefly see the previous account’s services, audit data, or admin results. Outstanding requests can repopulate those caches after logout.

**Fix:** Cancel queries and clear the QueryClient on logout and identity changes. Namespace sensitive query keys by org/user and test account switching with an in-flight request.

**Confidence:** High; verified; transient disclosure timing inferred.

### R33 — CI and tests do not enforce the production-readiness claims

**Severity: MEDIUM**

**Locations:** `.github/workflows/ci.yml:24–40`; `pyproject.toml:27–34,73–78`; `tests/conftest.py:41–60,73–80`; `tests/test_secret_hygiene.py:68–87,118`; `tests/jobs/test_retention.py:89–113`.

**Problem:** CI runs Python checks only. It does not build/typecheck/test the frontend or enforce the specified access/connections coverage target. Secret hygiene is lexical, with one tautological assertion. Database setup tests fresh migrations, and the shared MCP fixture avoids a known restart race.

**Production impact:** Green CI permits frontend regressions, populated-database upgrade failures, runtime secret logging, and OAuth concurrency/session-lifecycle defects.

**Fix:** Add frontend checks and measured coverage gates. Add regressions for R01–R16, especially populated migration upgrades, rotating-token concurrency, credential replacement on live sessions, audit recovery, cancellation, and second-org admin APIs. Guard destructive test URLs before database recreation.

**Confidence:** High; verified CI/test structure. Existing tests were not rerun.

### R34 — Service deletion leaves OAuth client secrets stored

**Severity: MEDIUM**

**Locations:** `src/cam/api/queries.py:355–373`; `src/cam/db/models.py:163`.

**Problem:** Soft deletion wipes connection secrets and refresh tokens but does not wipe `service.auth_secret_enc` or outstanding OAuth states.

**Production impact:** Deleted services retain usable OAuth application credentials, contrary to delete semantics. Pending callbacks compound this risk under R12.

**Fix:** Clear service-level encrypted secrets and revoke/delete pending authorization states in the same deletion transaction.

**Confidence:** High; verified.

### R35 — Pool startup, retries, and error classification have concurrency defects

**Severity: MEDIUM**

**Locations:** `src/cam/upstream/client.py:159–198,200–205,239–242`; `src/cam/upstream/pool.py:141–165`.

**Problem:** Session startup can retry internally and then be retried again by the pool, exceeding the permitted one reconnect. Cancellation while waiting for `_ready` does not reliably close the newly created owner task. Concurrent calls clear/drain one shared HTTP-status list.

**Production impact:** Establishment can take several timeout budgets; abandoned startup tasks can retain transports; one call’s 401/429/5xx can affect another call’s classification.

**Fix:** Own establishment retry policy in one layer. Close the owner task in cancellation/error cleanup. Correlate HTTP statuses per request rather than storing them in a session-wide mutable list.

**Confidence:** High for code structure; concurrency outcomes inferred.

### R36 — Request timeout and response-memory bounds are incomplete

**Severity: MEDIUM**

**Locations:** `src/cam/upstream/rest.py:238–249,288`; `src/cam/upstream/client.py:151–160,238–247`; `src/cam/invoke/pipeline.py:308–365`; `src/cam/upstream/oauth_http.py:43–54`.

**Problem:** REST uses HTTPX phase/inactivity timeouts rather than one end-to-end deadline, allowing redirects or slowly delivered bodies to exceed the configured timeout. Responses/specs are fully buffered before normalization and truncation. MCP establishment has a separate budget from the tool call.

**Production impact:** A 60-second service can occupy resources substantially longer. Huge responses can exhaust memory before the output cap is applied.

**Fix:** Apply an explicit dispatch deadline across DNS, establishment, redirects, and response reading. Stream HTTP bodies with hard byte limits and bound discovery/schema sizes. Preserve a clear distinction between “not dispatched” and “execution outcome unknown.”

**Confidence:** High; verified timeout/buffering implementation.

### R37 — Dependency reproducibility is incomplete at image and upgrade boundaries

**Severity: LOW**

**Locations:** `Dockerfile:1,11`; `pyproject.toml:8–25`; `.github/workflows/ci.yml:25–26`; `src/cam/upstream/pinning.py:17–18,87–99`; `uv.lock:697–719`.

**Problem:** Frozen Python and npm lockfiles are present, but image tags and CI actions are mutable. Dependency declarations have broad lower bounds. Pinning modifies private HTTP transport internals across both HTTP stacks.

**Production impact:** Base-image changes and dependency refreshes can silently break critical transport/security behavior even while application source remains unchanged.

**Fix:** Pin image digests and action SHAs, explicitly declare directly imported transport dependencies, and require transport compatibility/security regression tests during lockfile updates.

**Confidence:** High; verified. No current CVE or malicious dependency is asserted.

### R38 — Windows launcher does not pass bootstrap credentials into the container

**Severity: LOW**

**Locations:** `scripts/run_cam.ps1:108–111`; `scripts/run_cam.ps1:25–28,39–41`.

**Problem:** Setting host PowerShell environment variables does not inject them into `docker compose exec`; the command omits `-e`. Installing a copied launcher also changes `$PSScriptRoot`, so its default repo lookup points outside the repository.

**Production impact:** First-admin bootstrap and installed launcher usage fail under ordinary Windows use.

**Fix:** Pass explicit `-e CAM_ADMIN_EMAIL=…` and `-e CAM_ADMIN_PASSWORD=…`; persist or embed the installation’s repo path. Test on Windows.

**Confidence:** High; verified command behavior; Windows execution was not performed.

### R39 — Some accepted input values produce avoidable internal errors

**Severity: LOW**

**Locations:** `src/cam/api/routes/services.py:38–44`; `src/cam/api/queries.py:338–346,699–702`; `src/cam/api/routes/admin_users.py:40–43,134–136`.

**Problem:** Optional patch fields accept explicit nulls, but implementation converts them with `int(None)` or assigns null to non-null database columns. Cursor structure is checked, but datetime/UUID parsing errors are not translated into API validation errors.

**Production impact:** Validly parsed request bodies can trigger 500 responses rather than actionable 422 errors.

**Fix:** Reject null for non-nullable patch fields, preserve intentional null only for nullable properties, and catch cursor conversion failures as `ApiProblem("VALIDATION_ERROR", …, 422)`.

**Confidence:** High; verified.

### R40 — Required release gates remain incomplete

**Severity: INFO**

**Locations:** `docs/architecture.md:707–724`; `docs/security-review.md:199–239`.

**Problem:** Security sign-off, pilot completion, production TLS deployment, database backup/restore confirmation, secret-manager deployment, and company rollout remain unchecked. The security document contains questions rather than recorded approval.

**Production impact:** The repository has neither met its own whole-product definition of done nor supplied evidence that the intended production environment works.

**Fix:** Complete technical remediation, record security decisions against actual corrected behavior, execute the pilot, and attach deployment/recovery evidence before marking the release ready.

**Confidence:** High; verified document state.

## Assessment across the requested dimensions

| Dimension | Assessment |
|---|---|
| Async correctness | Async DB and HTTP clients are used; Argon2 is threaded. Schema reference retrieval and ZIP/YAML processing break request-path async safety: R01, R08. |
| Access control | Agent resolution generally uses `access/`; admin tenancy, duplicated dashboard rules, and dependency existence leaks remain: R02, R20, R25. |
| Secrets | Encryption and token hashing are present. Logging, redirect forwarding, key replacement, and incomplete deletion are blockers: R03–R05, R34. |
| URL policy / SSRF | Explicit clients implement address checks and pinning. Schema loaders bypass them; pooled MCP policy and unified registration checks are incomplete: R01, R18, R19. |
| Multi-tenancy | Tenant columns exist broadly. Cross-org admin operations are unsafe; query/foreign-key enforcement is incomplete: R02, R25. |
| Invoke contract | No application-level resend of `tools/call` was identified. Cap, timeout, audit durability, and post-dispatch failure handling violate important guarantees: R06, R07, R14, R35, R36. |
| Auth / OAuth | Password hashing, token hashes, cookie flags, CSRF comparison, and PKCE are implemented. Lockout, revocation, refresh, reconnect, and callback lifecycle need repair: R09–R12, R16, R17. |
| Validation / errors / rate limits | ORM queries generally use bound parameters. External-reference injection, process-local limits, raw errors, and null/cursor handling remain: R01, R04, R15, R39. |
| Database | Async pooling and Alembic are present. Populated upgrades, retention, tenant integrity, and migration deployment are deficient: R13, R23–R25, R30. |
| Concurrency / jobs | Pool establishment is serialized locally. Distributed coordination, refresh races, bounded probes, shutdown, and request error attribution need work: R09, R15, R27, R35. |
| Tests | Real Postgres and MCP/REST/OAuth fixtures exist. Important production scenarios and coverage enforcement are missing: R33. |
| Deployment / recovery | uv/frozen installs and resource limits exist. Cloud SQL migration, health semantics, hardening, backup scripts, and scaling are incomplete: R15, R29–R31. |
| Observability | Prometheus instrumentation exists. Audit success accounting, tool failures, heartbeat thresholds, and label cardinality are unreliable: R06, R28. |
| Dependencies | Python/npm locks improve reproducibility. Mutable images/actions and private transport internals remain risks: R37. |

## What could not be verified

- Current pytest results, integration coverage percentages, or frontend test/build results. Prior counts in `PROGRESS.md` are historical claims, not results from this review.
- Actual GKE manifests after placeholder replacement, ingress affinity, proxy-header trust, TLS, NetworkPolicy, IAM, and secret access controls.
- Production Postgres schema, query plans, pool utilization, backup schedules, PITR, or successful recovery of encrypted CAM data.
- Live Notion OAuth compatibility, real upstream tools, or Claude Code/Codex/Cursor behavior.
- Security-team approval, pilot use, offline key custody, or alert delivery.
- Absence of secrets across all git history or external logging systems. The tracked file inventory excludes `.env`; that alone is not a historical secret scan.
- Current dependency vulnerability status. No vulnerability database or package-provenance audit was performed.
- Windows behavior through execution; the launcher findings are static.

Assumptions: the supplied Docker/Kubernetes configuration and default logging are representative; production dependencies match the lockfile/installed versions inspected; no uncommitted external infrastructure layer already compensates for the identified defects. Where an external control could mitigate a finding, it must be demonstrated rather than assumed.

## Prioritized remediation table

Effort estimates include a focused regression test and are approximate engineer-days.

| Severity | ID | Primary file | One-line fix | Effort |
|---|---|---|---|---:|
| CRITICAL | R01 | `src/cam/invoke/validation.py:47` | Disable uncontrolled schema retrieval; resolve allowed refs through upstream policy. | 2–4 |
| CRITICAL | R02 | `src/cam/api/routes/admin_services.py:118` | Require authenticated org identity in every administrative service function. | 2–3 |
| CRITICAL | R03 | `scripts/provision_secrets_gsm.sh:10` | Make provisioning non-overwriting and implement safe key-ring rotation. | 1–3 |
| HIGH | R04 | `src/cam/api/main.py:174` | Redact/suppress transport logging and sanitize exception output. | 1–2 |
| HIGH | R05 | `src/cam/upstream/rest.py:243` | Bind credentials to origin and stop blind redirect operation replay. | 1–2 |
| HIGH | R06 | `src/cam/audit/writer.py:145` | Persist audit durably and count committed writes accurately. | 3–5 |
| HIGH | R08 | `src/cam/skills/validation.py:277` | Enforce expansion limits before ZIP reads and offload bounded parsing. | 1–2 |
| HIGH | R09 | `src/cam/connections/oauth.py:485` | Refresh ORM state under the OAuth row lock. | 1 |
| HIGH | R10 | `src/cam/upstream/pool.py:64` | Version credentials and invalidate stale pooled transports. | 2–3 |
| HIGH | R11 | `src/cam/registry/resync.py:106` | Include OAuth material during MCP discovery/resync. | 1 |
| HIGH | R12 | `src/cam/api/routes/connect.py:291` | Revalidate callback authorization/lifecycle and revoke pending states. | 1–2 |
| HIGH | R13 | `alembic/versions/b8d2f1a6c4e7_audit_logs_partitions.py:125` | Create historical partitions before copying populated audit rows. | 1–2 |
| HIGH | R14 | `src/cam/invoke/pipeline.py:359` | Keep health-write failures from replacing successful execution results. | 1 |
| HIGH | R15 | `deploy/gke/hpa.yaml:14` | Run one replica initially; add distributed state before scaling. | 1 initially; 5–10 for scaling |
| HIGH | R16 | `src/cam/auth/lockout.py:45` | Make login admission targeted, atomic, and bounded before Argon2. | 2–3 |
| HIGH | R07 | `src/cam/invoke/pipeline.py:366` | Cap structured JSON and errors together with text. | 2–3 |
| HIGH | R30 | `deploy/gke/migration-job.yaml:20` | Supply migration DB connectivity and remove app-startup migrations. | 1–2 |
| MEDIUM | R17 | `src/cam/auth/sessions.py:40` | Add session revocation/versioning. | 2 |
| MEDIUM | R18 | `src/cam/upstream/client.py:139` | Enforce current URL policy on each outbound request. | 2–3 |
| MEDIUM | R19 | `src/cam/registry/services.py:560` | DNS-validate unified personal registration. | 0.5–1 |
| MEDIUM | R20 | `src/cam/skills/store.py:52` | Centralize access rules and opaque dependency resolution. | 2–3 |
| MEDIUM | R21 | `src/cam/invoke/pipeline.py:242` | Populate audit credential-owner attribution. | 0.5–1 |
| MEDIUM | R22 | `src/cam/api/routes/admin_diagnostics.py:95` | Query audit entries with org/id predicates. | 0.5 |
| MEDIUM | R23 | `src/cam/api/queries.py:943` | Wire a consistent per-org effective-settings resolver. | 2–3 |
| MEDIUM | R24 | `src/cam/jobs/retention.py:86` | Delete expired rows within the boundary partition. | 1–2 |
| MEDIUM | R25 | `src/cam/db/models.py:181` | Add explicit tenant predicates and tenant-consistent foreign keys. | 3–5 |
| MEDIUM | R26 | `src/cam/access/resolution.py:387` | Resolve template owners explicitly and notify discovery changes. | 3–5 |
| MEDIUM | R27 | `src/cam/jobs/health.py:164` | Bound probes, preserve connection health, and await job shutdown. | 2–3 |
| MEDIUM | R28 | `src/cam/api/metrics.py:108` | Instrument tool outcomes and correct labels/heartbeat alerts. | 1–2 |
| MEDIUM | R29 | `Dockerfile:11` | Harden container/network defaults and separate liveness/readiness. | 1–2 |
| MEDIUM | R31 | `deploy/in-cluster-postgres/backup-cronjob.yaml:25` | Repair backup commands and prove full CAM-plus-key recovery. | 2–3 |
| MEDIUM | R32 | `web/src/lib/auth.tsx:85` | Cancel/clear caches on identity changes and namespace query keys. | 1 |
| MEDIUM | R33 | `.github/workflows/ci.yml:24` | Add frontend/coverage checks and production regression scenarios. | 3–5 |
| MEDIUM | R34 | `src/cam/api/queries.py:355` | Wipe service OAuth secrets and authorization states on deletion. | 0.5–1 |
| MEDIUM | R35 | `src/cam/upstream/client.py:181` | Unify retries, clean cancelled startup, and correlate status per call. | 2–3 |
| MEDIUM | R36 | `src/cam/upstream/rest.py:238` | Enforce overall deadlines and streaming byte limits. | 2–3 |
| LOW | R37 | `Dockerfile:1` | Pin images/actions and gate private-transport compatibility updates. | 1 |
| LOW | R38 | `scripts/run_cam.ps1:110` | Pass bootstrap env explicitly and preserve installed repo location. | 0.5–1 |
| LOW | R39 | `src/cam/api/queries.py:343` | Validate null patch values and cursor conversions. | 0.5–1 |
| INFO | R40 | `docs/architecture.md:707` | Complete security, pilot, deployment, and recovery release gates. | Owner-dependent |

## Overall verdict and top five blockers

**Production-ready: No.**

The top five blockers are:

1. **Uncontrolled schema retrieval enables SSRF and synchronous network access** — R01, `src/cam/invoke/validation.py:47–65`.
2. **Administrative APIs cross tenant boundaries** — R02, `src/cam/api/routes/admin_services.py:118–137`.
3. **Credentials can leak through transport logs and redirects** — R04/R05, `src/cam/api/main.py:174`, `src/cam/upstream/rest.py:243–268`.
4. **The provisioning workflow can invalidate every stored credential** — R03, `scripts/provision_secrets_gsm.sh:10–42`.
5. **Audit logging loses executed-call records during ordinary failure conditions** — R06, `src/cam/audit/writer.py:145–201`.

After those are repaired, OAuth/session correctness, populated-database upgrades, deployment connectivity, and the unfulfilled security/pilot gates still require validation before rollout.
