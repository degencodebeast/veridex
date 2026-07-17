# Veridex Arena — Backup & Restore (D-1)

Operator guide for backing up and restoring the durable state of the deployed stack, plus the
repeatable **restore drill** that proves a backup is restorable *before* the demo depends on it.
Pairs with `runbook.md` §10. Everything below is a LOCAL/operator procedure — this repo performs no
production DNS/VPS/credential mutation.

## Durability surfaces

| Surface | What lives there | Volume | Loss = |
|---|---|---|---|
| Postgres | runs, competitions, event log, execution records, deployment attempts, **`runtime_events`** (AgentOS session/OPS spool) | `postgres-data` | the proof record + Agent-Ops history |
| WAL spool | I-4 crash-safe Agent-Ops WAL (`WAL_DIR=/data/wal`) | `wal-spool` | in-flight ops events not yet committed (AC-13) |
| ReplayPack capture | live-captured packs (R-0b/R-2) | `replay-capture` | captured replay artifacts |

All three are **named volumes** in `compose.coolify.yml` and survive container replacement. Never
mount them `tmpfs`; never bind them to ephemeral paths.

## Backup (daily, operator-scheduled in Coolify)

1. Schedule a daily `pg_dump` of `postgres-data` with **7-day retention** (Coolify scheduled task):

   ```sh
   pg_dump -Fc -f /backups/veridex-$(date +%F).dump "$DATABASE_URL"
   ```

   Custom format (`-Fc`) is compressed and works with selective `pg_restore`.

2. The `wal-spool` and `replay-capture` volumes are durable by construction (named volumes). Snapshot
   them with your VPS/Coolify volume-backup mechanism if you want point-in-time capture history;
   Postgres is the authoritative proof record, so the DB dump is the primary backup.

## Restore

Restore a snapshot into a fresh database, then repoint `DATABASE_URL`:

```sh
createdb -h "$PGHOST" -U "$PGUSER" veridex_restored
pg_restore -h "$PGHOST" -U "$PGUSER" -d veridex_restored /backups/veridex-YYYY-MM-DD.dump
# verify a sample row, then update DATABASE_URL in Coolify to point at veridex_restored
```

## Restore drill (run once before the demo)

`scripts/restore_drill.sh` is the automated, self-contained proof that a backup round-trips:
seed a known row → `pg_dump -Fc` → `pg_restore -l` **dry-run** (validates the archive TOC without
mutating anything) → restore into a **fresh** database → read the sample row back → drop the scratch
database. It reads standard libpq env (`PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE`, falling back to
`POSTGRES_*`).

Run it inside the pinned Postgres container (which carries `pg_dump`/`pg_restore`/`psql`):

```sh
docker compose -f compose.coolify.yml exec -T \
  -e PGUSER="$POSTGRES_USER" -e PGPASSWORD="$POSTGRES_PASSWORD" -e PGDATABASE="$POSTGRES_DB" \
  postgres sh -s < scripts/restore_drill.sh
```

Success prints `RESTORE_DRILL_OK: sample row read back from fresh restore: <note>` and exits 0.
`tests/test_d1_compose_deploy.py::TestRestoreDrill` runs exactly this against the local stack.

## Readiness after restore

After a restore, confirm the stack is serving with the deployment readiness probe (deeper than
`/healthz` liveness — it checks Postgres + the AgentOS session DB + the ReplayPack catalog):

```sh
curl -fsS http://<host>:8000/readyz    # 200 {"ready": true, ...}; 503 fail-closed if any is down
./scripts/smoke_public.sh              # BASE_URL=… liveness + readiness + durable path + authz
```
