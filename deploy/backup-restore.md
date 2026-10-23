# Backup / restore — tested procedure (Phase 8.2)

The "tested restore" requirement is met here, against the compose Postgres.
The scripts that prove it live in `scripts/` and run green.

## The tested restore cycle

`sh scripts/backup_restore_test.sh` (Postgres at `localhost:5432`, default
user `cam`, password `cam`; uses the `cam-db-1` compose container when present,
else the local `psql`). What it does:

1. Creates a scratch database `cam-backup-restore-test-<pid>` (never touches
   `cam` or `cam_test`), creates a table and seeds 257 rows.
2. Records `count(*)` and a payload checksum before anything else.
3. `pg_dump -Fd` (directory format, same layout as a Cloud SQL export).
4. Truncates (wipe) and asserts 0 rows remain.
5. Drops the database, recreates it empty.
6. `pg_restore` the dump into the fresh database.
7. Asserts row count and checksum match exactly.

Both the container path and the local-`psql` path are exercised by the same
helpers; every `psql`/`pg_dump`/`pg_restore` call passes `-U "$PGUSER"` and the
target database explicitly.

Output (this repo):

```
seeded: rows|sum = 257|<checksum>
wiped: 0 rows left
restored: rows|sum = 257|<checksum>
backup/restore cycle PASSED
```

The checksum is data-dependent (257 rows of random `md5`), so the exact number
changes every run; `BEFORE == AFTER` is the assertion that matters.

## Ad-hoc backup and restore

```sh
sh scripts/backup_db.sh cam /tmp/cam.dump          # pg_dump -Fd of the cam DB
sh scripts/restore_db.sh /tmp/cam.dump cam-restore # create + pg_restore
```

Notes (all fixed under R31):

- the database name is a **bare identifier** — pass `cam`, not `/cam`. A
  leading slash is read by `pg_dump` as a file path and the dump fails.
- the container branches pass `-U "$PGUSER"` explicitly, because the ambient
  `PGUSER` does not cross into `docker exec` (the postgres image's superuser
  role is `POSTGRES_USER = cam`).
- `restore_db.sh` **quotes** the created database name (`CREATE DATABASE
  "<name>"`, embedded quotes doubled) instead of interpolating it raw into SQL.
- `PGHOST` / `PGPORT` / `PGUSER` / `PGPASSWORD` / `PG_CONTAINER` all override
  the defaults.
- The container path uses the `postgres:16-alpine` client **inside** the DB
  container, so client and server major versions always match. The local-`psql`
  fallback uses whatever client tools are on `PATH`; those must match the
  server's major version (e.g. a `pg_dump`/`pg_restore` from a newer major
  emits `SET transaction_timeout`, which a PG16 server rejects). Prefer the
  container path, or install matching client tools.

## In-cluster CronJob

`deploy/in-cluster-postgres/backup-cronjob.yaml` dumps nightly with
`pg_dump -h cam-postgres -U cam -Fc ... -d cam`. The previous command passed
the database name as `/cam`, which failed; it now passes the bare `cam`
identifier (R31).

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

## CAM-level recovery verification (owner, mandatory drill)

The local cycle above proves Postgres dump/restore mechanics only. A CAM
recovery is not proven until a restored database **plus the offline master
keys** serve a real login and decrypt a stored credential. Do this drill before
rollout and after each migration release:

1. Restore the latest backup into the scratch probe instance (Cloud SQL) or a
   scratch database (compose).
2. Point a throwaway CAM instance at it with the **offline
   `CAM_ENCRYPTION_KEYS` / `CAM_ENCRYPTION_KEY_CURRENT`** (never a freshly
   generated ring — see `encryption-keys.md`).
3. Log a user in (proves session secret + users table) and invoke one service
   whose credential is stored encrypted (proves the data key unwraps under the
   restored master key).
4. Record the date, the restore run id and the outcome in the runbook.

## Notes

- backups and retention are set on the Cloud SQL instance (see `cloudsql.md`)
- restore into a scratch probe instance; delete it after each real test
- using in-cluster Postgres instead? `in-cluster-postgres/` CronJob dumps
  nightly onto its own PVC
