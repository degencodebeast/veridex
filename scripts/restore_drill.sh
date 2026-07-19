#!/bin/sh
# restore_drill.sh — D-1 backup/restore drill (runbook §10 / v2_infra §7).
#
# Proves the Postgres backup is genuinely restorable BEFORE the demo depends on it:
#   1. seed a known sample row,
#   2. `pg_dump -Fc` a custom-format snapshot,
#   3. `pg_restore -l` DRY-RUN — list the archive TOC (proves the dump is well-formed/restorable,
#      mutating nothing),
#   4. restore into a FRESH database and read the sample row back,
#   5. drop the scratch database.
#
# POSIX sh so it runs inside the pinned postgres:16.6-alpine container (which carries
# pg_dump/pg_restore/psql/createdb/dropdb). Connection comes from standard libpq env:
#   PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE  (falling back to POSTGRES_* / localhost).
#
# Prints RESTORE_DRILL_OK + the read-back value on success; exits non-zero on any failure.
set -eu

PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-${POSTGRES_USER:-veridex}}"
PGPASSWORD="${PGPASSWORD:-${POSTGRES_PASSWORD:-}}"
SRC_DB="${PGDATABASE:-${POSTGRES_DB:-veridex}}"
export PGHOST PGPORT PGUSER PGPASSWORD

DRILL_DB="veridex_restore_drill"
DUMP="/tmp/veridex_restore_drill.dump"
TOC="/tmp/veridex_restore_drill.toc"
PROBE_NOTE="restore-drill-$(date +%s)-$$"

echo "restore-drill: host=$PGHOST db=$SRC_DB user=$PGUSER"

# 1) Seed a known sample row (dedicated probe table — no coupling to app schema/data).
psql -d "$SRC_DB" -v ON_ERROR_STOP=1 -q -c \
  "CREATE TABLE IF NOT EXISTS restore_drill_probe (id serial PRIMARY KEY, note text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());"
psql -d "$SRC_DB" -v ON_ERROR_STOP=1 -q -c \
  "INSERT INTO restore_drill_probe (note) VALUES ('$PROBE_NOTE');"
echo "  seeded sample row: $PROBE_NOTE"

# 2) Backup — custom-format snapshot (the format Coolify's daily pg_dump uses).
pg_dump -Fc -f "$DUMP" "$SRC_DB"
echo "  pg_dump -> $DUMP ($(wc -c < "$DUMP" | tr -d ' ') bytes)"

# 3) DRY-RUN — list the archive table-of-contents. Proves the dump is restorable; mutates nothing.
pg_restore -l "$DUMP" > "$TOC"
grep -q "restore_drill_probe" "$TOC" || { echo "RESTORE_DRILL_FAIL: probe table absent from dump TOC" >&2; exit 1; }
echo "  pg_restore -l (dry-run) OK: $(wc -l < "$TOC" | tr -d ' ') TOC entries"

# 4) Restore into a FRESH database and read the sample row back.
dropdb --if-exists "$DRILL_DB"
createdb "$DRILL_DB"
pg_restore -d "$DRILL_DB" "$DUMP"
READBACK="$(psql -d "$DRILL_DB" -t -A -v ON_ERROR_STOP=1 -c \
  "SELECT note FROM restore_drill_probe WHERE note = '$PROBE_NOTE' LIMIT 1;")"

if [ "$READBACK" != "$PROBE_NOTE" ]; then
  echo "RESTORE_DRILL_FAIL: read-back '$READBACK' != expected '$PROBE_NOTE'" >&2
  dropdb --if-exists "$DRILL_DB" || true
  exit 1
fi

# 5) Clean up the scratch database.
dropdb --if-exists "$DRILL_DB"

echo "RESTORE_DRILL_OK: sample row read back from fresh restore: $READBACK"
