#!/bin/sh
set -eu

DB="${1:?usage: backup_db.sh <database> [output.dump]}"
OUT="${2:-./cam-backup-$(printf '%s' "$DB" | tr -c 'A-Za-z0-9._-' '_').dump}"
CONTAINER="${PG_CONTAINER:-cam-db-1}"

export PGHOST="${PGHOST:-localhost}"
export PGPORT="${PGPORT:-5432}"
export PGUSER="${PGUSER:-cam}"
export PGPASSWORD="${PGPASSWORD:-cam}"

# The DB name must be passed as a plain identifier (no leading slash): a value
# like "/cam" is read by pg_dump as file path and fails. Pass e.g. `cam`.
if docker inspect "$CONTAINER" >/dev/null 2>&1; then
  # Inside the container the ambient PGUSER does not carry over, so pass -U
  # explicitly (the postgres image's superuser role is POSTGRES_USER = cam).
  docker exec -e PGPASSWORD "$CONTAINER" \
    pg_dump -U "$PGUSER" -Fd --no-owner --no-privileges -f "/tmp/cam-backup-$$.dump" -d "$DB"
  docker cp "$CONTAINER:/tmp/cam-backup-$$.dump" "$OUT"
  docker exec "$CONTAINER" rm -rf "/tmp/cam-backup-$$.dump"
else
  pg_dump -Fd --no-owner --no-privileges -f "$OUT" -d "$DB"
fi
echo "backup written: $OUT"
