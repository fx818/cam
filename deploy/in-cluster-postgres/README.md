This directory documents the alternative to Cloud SQL (Phase 8.2):

- Run Postgres 16 as a StatefulSet in the `cam` namespace with a PVC and the
  `cam-postgres`/`cam-postgres-backups` claims. Point `CAM_DATABASE_URL` at
  the in-pod DNS (`postgresql+asyncpg://cam:pw@cam-postgres:5432/cam`).
- `backup-cronjob.yaml`: nightly `pg_dump -Fc` to a separate PVC (scheduled
  17:30 UTC = 23:00 IST). Manual restore with `pg_restore` per
  `deploy/backup-restore.md`.

Cloud SQL remains the recommended option (Decision log) because it ships
verified nightly backups, point-in-time recovery, read replicas and IAM
auth without another StatefulSet to operate. Choose this only if Cloud SQL
is not an option; then backups and ransomware/pruning policies are the
owner's responsibility to verify weekly.
