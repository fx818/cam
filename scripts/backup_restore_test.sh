#!/bin/sh
set -eu

CONTAINER="${PG_CONTAINER:-cam-db-1}"
DB="cam-backup-restore-test-$$"
DUMP="./cam-restore-test-$$"
PGPASSWORD="${PGPASSWORD:-cam}"
PGUSER="${PGUSER:-cam}"
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
export PGPASSWORD PGUSER PGHOST PGPORT

psql_run() {
  target="$1"
  sql="$2"
  if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    printf '%s' "$sql" | docker exec -i -e PGPASSWORD="$PGPASSWORD" "$CONTAINER" psql -At -U cam -d "$target"
  else
    printf '%s' "$sql" | psql -At
  fi
}

sql() {
  psql_run "$DB" "$1"
}

drop_test_db() {
  psql_run postgres "DROP DATABASE IF EXISTS \"$DB\"" >/dev/null
}

dump_db() {
  docker exec -e PGPASSWORD="$PGPASSWORD" "$CONTAINER" rm -rf /tmp/restore-test.dump
  docker exec -e PGPASSWORD="$PGPASSWORD" "$CONTAINER" \
    pg_dump -U cam -Fd --no-owner --no-privileges -f /tmp/restore-test.dump "$DB"
  docker cp "$CONTAINER:/tmp/restore-test.dump" "$DUMP"
  docker exec "$CONTAINER" rm -rf /tmp/restore-test.dump
}

restore_db() {
  docker cp "$DUMP" "$CONTAINER:/tmp/restore-test.in"
  docker exec -e PGPASSWORD="$PGPASSWORD" "$CONTAINER" \
    pg_restore -U cam --no-owner --no-privileges --no-tablespaces --exit-on-error \
    -d "$DB" /tmp/restore-test.in
  docker exec "$CONTAINER" rm -rf /tmp/restore-test.in
}

trap 'rm -rf "$DUMP"; drop_test_db' EXIT

psql_run postgres "CREATE DATABASE \"$DB\"" >/dev/null

sql "CREATE TABLE restore_test_rows (id integer PRIMARY KEY, payload text NOT NULL)" >/dev/null
sql "INSERT INTO restore_test_rows SELECT g, md5(random()::text) FROM generate_series(1, 257) g" >/dev/null

BEFORE=$(sql "SELECT count(*) || '|' || sum(hashtext(payload)::bigint) FROM restore_test_rows")
echo "seeded: rows|sum = $BEFORE"

dump_db

sql "TRUNCATE restore_test_rows" >/dev/null
WIPED=$(sql "SELECT count(*) FROM restore_test_rows")
[ "$WIPED" = "0" ] || { echo "FAIL: wipe left $WIPED rows"; exit 1; }
echo "wiped: 0 rows left"

drop_test_db
psql_run postgres "CREATE DATABASE \"$DB\"" >/dev/null

restore_db

AFTER=$(sql "SELECT count(*) || '|' || sum(hashtext(payload)::bigint) FROM restore_test_rows")
[ "$AFTER" = "$BEFORE" ] || { echo "FAIL: before=$BEFORE after=$AFTER"; exit 1; }

echo "restored: rows|sum = $AFTER"
echo "backup/restore cycle PASSED"
