#!/bin/sh
set -eu

CONTAINER="${PG_CONTAINER:-cam-db-1}"
DB="cam-backup-restore-test-$$"
DUMP="./cam-restore-test-$$"
export PGHOST="${PGHOST:-localhost}"
export PGPORT="${PGPORT:-5432}"
export PGUSER="${PGUSER:-cam}"
export PGPASSWORD="${PGPASSWORD:-cam}"

# Both the docker (compose Postgres) and the direct local-psql paths are
# supported; every helper below branches the same way (R31: the non-docker
# branch used to ignore its target database).
have_container() { docker inspect "$CONTAINER" >/dev/null 2>&1; }

psql_run() {
  target="${1:-postgres}"
  sql="$2"
  if have_container; then
    printf '%s' "$sql" | docker exec -i -e PGPASSWORD "$CONTAINER" psql -At -U "$PGUSER" -d "$target"
  else
    printf '%s' "$sql" | psql -At -U "$PGUSER" -d "$target"
  fi
}

sql() {
  psql_run "$DB" "$1"
}

drop_test_db() {
  psql_run postgres "DROP DATABASE IF EXISTS \"$DB\"" >/dev/null
}

dump_db() {
  if have_container; then
    docker exec -e PGPASSWORD "$CONTAINER" rm -rf /tmp/restore-test.dump
    docker exec -e PGPASSWORD "$CONTAINER" \
      pg_dump -U "$PGUSER" -Fd --no-owner --no-privileges -f /tmp/restore-test.dump -d "$DB"
    docker cp "$CONTAINER:/tmp/restore-test.dump" "$DUMP"
    docker exec "$CONTAINER" rm -rf /tmp/restore-test.dump
  else
    rm -rf "$DUMP"
    pg_dump -U "$PGUSER" -Fd --no-owner --no-privileges -f "$DUMP" -d "$DB"
  fi
}

restore_db() {
  if have_container; then
    docker cp "$DUMP" "$CONTAINER:/tmp/restore-test.in"
    docker exec -e PGPASSWORD "$CONTAINER" \
      pg_restore -U "$PGUSER" --no-owner --no-privileges --no-tablespaces --exit-on-error \
      -d "$DB" /tmp/restore-test.in
    docker exec "$CONTAINER" rm -rf /tmp/restore-test.in
  else
    pg_restore -U "$PGUSER" --no-owner --no-privileges --no-tablespaces --exit-on-error -d "$DB" "$DUMP"
  fi
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
