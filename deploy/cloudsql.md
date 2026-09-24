# Postgres on Cloud SQL — provisioning, backups, restore (Phase 8.2)

Cloud SQL is the recommended Postgres home (Decision log: "Local Postgres,
later Cloud SQL"). The in-cluster alternative is documented under
`deploy/in-cluster-postgres/`.

## Provisioning (owner runs)

```sh
gcloud sql instances create cam-db-instance \
  --database-version=POSTGRES_16 \
  --tier=db-custom-2-7680 \
  --region=asia-south1 \
  --storage-auto-increase \
  --backup-start-time=17:30 \
  --enable-point-in-time-recovery \
  --maintenance-window-day=SUN --maintenance-window-hour=9

gcloud sql databases create cam --instance=cam-db-instance
gcloud sql users set-password cam --instance=cam-db-instance --password=STRONG_PASSWORD
```

- Backups: nightly automatic backup plus point-in-time recovery (the flags
  above) — this replaces hand-rolled dump schedules.
- Availability: ZONAL to start; switch to REGIONAL (automatic failover)
  before the company-wide rollout of 8.6:
  `gcloud sql instances patch cam-db-instance --availability-type=REGIONAL`.
- Network: enable private IP on the instance VPC; the `cloud-sql-proxy`
  sidecar in `deploy/gke/deployment.yaml` is created with `--private-ip`.

Connect string for `CAM_DATABASE_URL` (through the in-pod proxy):

`postgresql+asyncpg://cam:STRONG_PASSWORD@127.0.0.1:5432/cam`

Migrations: `deploy/gke/migration-job.yaml` runs `alembic upgrade head` as a
K8s Job before each deploy of a new image. Schema changes only through
Alembic (AGENTS.md rule).

## Backups

- **Cloud SQL (recommended):** automatic nightly backups + WAL with PITR,
  retained backups count configurable (default 7). Verify once that the
  first backup exists: console → SQL → the instance → Backups.
- **In-cluster alternative:** `deploy/in-cluster-postgres/backup-cronjob.yaml`
  runs a nightly `pg_dump -Fc` of the Postgres StatefulSet to a separate
  PVC. Restores are manual `pg_restore`s (same procedure as
  `backup-restore.md`) and must be tested before rolling out.

## Tested restore

The procedure is actually executed and verified here against the compose
Postgres — `scripts/backup_restore_test.sh` (details in
`deploy/backup-restore.md`). Cloud SQL equivalents:

1. Export nightly (optional on top of snapshots):
   `gcloud sql export sql cam-db-instance gs://bucket/cam-$(date +%F).sql.gz -d cam`.
2. Restore into a scratch instance when a real restore is needed:
   `gcloud sql backups restore --backup-run-id=ID --restore-instance=cam-restore-probe --instance=cam-db-instance`
   (exact sub-commands per the day's gcloud docs).
3. Verify with the same syntax-agnostic row-count checks as the local test
   (users, connections, services, latest `audit_logs` partitions) before
   anything points at the restored instance.
4. Point the app at the restored instance by rotating the `cam-db` Secret:
   ```sh
   kubectl create secret generic cam-db -n cam \
     --from-literal CAM_DATABASE_URL="postgresql+asyncpg://cam:pw@127.0.0.1:5432/cam" \
     --dry-run=client -o yaml | kubectl apply -f -
   kubectl rollout restart deployment/cam -n cam
   ```
5. Re-run this repo's `scripts/backup_restore_test.sh` against the restored
   instance bytes whenever new migrations land.

## DB pool sizing (Phase 7.2 finding)

The load test showed per-request DB round trips dominating latency with the
default pool (5 + 10 overflow) at 100+ concurrent agents. The two new env
vars calibrate the asyncpg pool to expected concurrency:

- `CAM_DB_POOL_SIZE` (default 10, set to 20 in gke configmap)
- `CAM_DB_MAX_OVERFLOW` (default 20, set to 40)

Size against expected peak concurrency (agents × in-flight invokes) and the
Postgres `max_connections` budget; Cloud SQL default allows hundreds.
