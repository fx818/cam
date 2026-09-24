#!/bin/sh
set -eu

DB="${1:?usage: backup_db.sh <database> [output.dump]}"
OUT="${2:-./cam-backup-$(printf '%s' "$DB" | tr '/' '_').dump}"
CONTAINER="${PG_CONTAINER:-cam-db-1}"

export PGHOST="${PGHOST:-localhost}"
export PGPORT="${PGPORT:-5432}"
export PGUSER="${PGUSER:-cam}"
export PGPASSWORD="${PGPASSWORD:-cam}"

if docker inspect "$CONTAINER" >/dev/null 2>&1; then
  docker exec -e PGPASSWORD "$CONTAINER" pg_dump -Fd --no-owner --no-privileges -f "/tmp/$$.dump" "$DB"
  docker cp "$CONTAINER:/tmp/$$.dump" "$OUT"
  docker exec "$CONTAINER" rm -rf "/tmp/$$.dump"
else
  pg_dump -Fd --no-owner --no-privileges -f "$OUT" "$DB"
fi
echo "backup written: $OUT"
