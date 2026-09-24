# deploy/ — server deployment artifacts (Phase 8)

What is here:

| Path | Purpose |
| --- | --- |
| `gke/` | Kubernetes manifests + the exact `gcloud`/`kubectl` commands the owner runs |
| `cloudsql.md` | Cloud SQL provisioning, backups and restore |
| `backup-restore.md` | The TESTED local restore procedure and its Cloud SQL equivalents |
| `encryption-keys.md` | Master-key provisioning into secret managers, rotation, offline backup |
| `grafana/` | Dashboard JSON + alert rules to import into Grafana/Prometheus |
| `in-cluster-postgres/` | Alternative to Cloud SQL: nightly `pg_dump` CronJob |
| `README.md` (this file) | Env-var list, rollout runbook (8.6), go-live checklist |

CAM is one stateless FastAPI container serving `/mcp`, `/api` and the built React
dashboard from the same origin. Postgres is Cloud SQL (recommended) or in-cluster.
Only env vars differ from local.

## Owner-supplied variables at deploy time

Set in the `cam-config` ConfigMap, the `cam-secrets` / `cam-db` Secrets, or via
Secret Manager (see `encryption-keys.md`):

| Variable | Owner value | Notes |
| --- | --- | --- |
| `CAM_BASE_URL` | e.g. `https://cam.indiamart.example` | Public URL; used for OAuth redirects and agent config snippets |
| `CAM_DATABASE_URL` | `postgresql+asyncpg://cam:STRONG_PASSWORD@127.0.0.1:5432/cam` | Through the Cloud SQL proxy sidecar; goes in `cam-db` Secret |
| `CAM_ENCRYPTION_KEYS` | `v1:<base64>,…` | Generate per `encryption-keys.md`; lose these and every credential must be re-entered |
| `CAM_ENCRYPTION_KEY_CURRENT` | `v1` | Must name a key above |
| `CAM_SESSION_SECRET` | 64+ random chars | Signs dashboard sessions |

Everything else has production-safe defaults in `deploy/gke/configmap.yaml`
(CAM_ENV=prod, one uvicorn worker, DB pool 20+40, spec timeouts/caps/retention).

## Rollout runbook (Phase 8.6)

**Sequence:**

1. Owner executes the go-live checklist in `gke/README.md` (project, GKE
   cluster, Artifact Registry image push, Cloud SQL, secrets, migration, deploy,
   DNS + TLS, Grafana wiring).
2. Admin-only bootstrapping: `kubectl run cam-create-admin …` (or
   `docker run … uv run cam create-admin`) to create the first admin; log in,
   register the global services (Redash, PNS, Windmill, Grafana MCPs), Notion
   template, and skills on the server; import the Grafana dashboard.
3. Pilot first. The two pilot teams (Phase 7.5) connect and generate tokens on
   the server instance, run Claude Code/Codex/Cursor against it, and are
   monitored for a week against the pilot-plan success criteria.
4. Team-by-team rollout in waves: (a) agent-platform/CDN teams dependent on the
   pilot services, (b) data-heavy teams (Redash/PNS/Windmill), (c) everyone
   else. Per wave: invite the wave's leads, admin creates their accounts from
   the org's roster, leads follow `docs/onboarding.md`.
5. Comms per wave: short announce (what CAM is, one MCP entry point, that
   agents must use `cam` for external calls, date of local-install retirement),
   plus the register-a-service guide link. Announce in the primary company chat,
   10 minutes of "office hours" per wave at roll-out.
6. Cutover checklist per team (all yes before decommissioning their local
   installs): accounts created, agents connect to the server `<CAM_BASE_URL>/mcp`
   with a labelled personal token, one real invoke verified per member, their
   key services re-registered as personal/global services on the server, audit
   rows visible in the dashboard.
7. Retirement of local installs: after each wave's checklist passes, remove
   that team's local `docker compose` instances and direct MCP config entries
   (duplicates of CAM services must be removed from agent configs so calls go
   through CAM's audit log). Keep the pilot's local instances until 8.6 fully
   lands, then shut down.
8. Final: server is the only CAM instance. Monitor `cam_scheduler_*`,
   `cam_services_by_health` and error-rate alerts for one quiet week before
   closing Phase 8.

**Rollback:** scale the Deployment to the previous image tag; database never
rolled back (migrations are additive). If Cloud SQL retries fail, restore the
nightly backup per `backup-restore.md`.

## Go-live checklist (summary; exact commands in gke/README.md)

- [ ] GKE project + cluster reachable (\`cluster owner\`)
- [ ] Artifact Registry push of `cam:<tag>` (built by multi-stage Dockerfile)
- [ ] Cloud SQL instance created; `cam` database; backups ON; IAM auth
- [ ] Secrets: `CAM_ENCRYPTION_KEYS`, `CAM_ENCRYPTION_KEY_CURRENT`,
      `CAM_SESSION_SECRET`, `CAM_DATABASE_URL` (offline-backup the keys first)
- [ ] `kubectl apply` of gke manifests; migration job; deploy
- [ ] `CAM_BASE_URL` DNS + TLS issued (cert-manager or GCP managed cert)
- [ ] Grafana dashboard + PrometheusRule imported; alerts verified
- [ ] Metrics/logs scrape confirmed (`/api/metrics`, container logs)
- [ ] First admin created; org services, template and skills configured
