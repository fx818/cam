#!/bin/sh
set -eu

DUMP="${1:?usage: restore_db.sh <dump-file> <target-database>}"
DB="${2:?usage: restore_db.sh <dump-file> <target-database>}"
CONTAINER="${PG_CONTAINER:-cam-db-1}"

export PGHOST="${PGHOST:-localhost}"
export PGPORT="${PGPORT:-5432}"
export PGUSER="${PGUSER:-cam}"
export PGPASSWORD="${PGPASSWORD:-cam}"

# Quote the identifier so names with dashes/uppercase survive; double embedded
# quotes per SQL rules (R31: never interpolate a raw DB name into SQL).
DB_IDENT="$(printf '%s' "$DB" | sed 's/"/""/g')"

create_target() {
  if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    printf 'CREATE DATABASE "%s"\n' "$DB_IDENT" \
      | docker exec -i -e PGPASSWORD "$CONTAINER" psql -U "$PGUSER" -d postgres -q
  else
    createdb "$DB"
  fi
}

restore() {
  if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    docker cp "$DUMP" "$CONTAINER:/tmp/cam-restore.dump"
    docker exec -e PGPASSWORD "$CONTAINER" \
      pg_restore --no-owner --no-privileges --no-tablespaces --exit-on-error \
      -U "$PGUSER" -d "$DB" /tmp/cam-restore.dump
    docker exec "$CONTAINER" rm -rf /tmp/cam-restore.dump
  else
    pg_restore --no-owner --no-privileges --no-tablespaces --exit-on-error -d "$DB" "$DUMP"
  fi
}

create_target
restore
echo "restored into: $DB"
