# Backup / restore — tested procedure (Phase 8.2)

The "tested restore" requirement is met here, against the compose Postgres.
The script that proves it exists in `scripts/` and runs green:

## The tested restore cycle

`sh scripts/backup_restore_test.sh` (Postgres at `localhost:5432`, default
user `cam`, password `cam`; points at the `cam-db-1` compose container when
present, else the local `psql`). What it does:

1. Creates a scratch database `cam-backup-restore-test-<pid>` (never touches
   `cam` or `cam_test`), creates a table and seeds 257 rows.
2. Records `count(*)` and a payload checksum before anything else.
3. `pg_dump -Fd` (directory format, same layout as a Cloud SQL export).
4. Truncates (wipe) and asserts 0 rows remain.
5. Drops the database, recreates it empty.
6. `pg_restore` the dump into the fresh database.
7. Asserts row count and checksum match exactly.

Output (this repo, 2026-09-24):

```
seeded: rows|sum = 257|9993949730
wiped: 0 rows left
restored: rows|sum = 257|9993949730
backup/restore cycle PASSED
```

## Cloud SQL equivalents

Every step above maps to a manual or automated step on Cloud SQL:

| Local step | Cloud SQL |
| --- | --- |
| `pg_dump` to a local file | instance has auto backup + PITR; or `gcloud sql export sql` |
| truncate | not needed (clone restores into a new instance) |
| drop + recreate | `gcloud sql instances create cam-restore-probe` then restore into it |
| `pg_restore` | `gcloud sql backups restore --backup-run-id=ID --restore-instance=cam-restore-probe --instance=cam-db-instance` |
| count + checksum verification | same SQL checks before repointing the app |

A restore is not validated until the checks in the last row ran. Verify
nightly backups exist once, and exercise a full restore at least once before
company rollout and again after each migration release.

## Notes

- backups and retention are set on the Cloud SQL instance (see `cloudsql.md`)
- restore into a scratch probe instance; delete it after each real test
- using in-cluster Postgres instead? `in-cluster-postgres/` CronJob dumps
  nightly onto its own PVC
