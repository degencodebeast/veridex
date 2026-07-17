# Veridex Arena — Provisioning Inventory (D-0)

Records WHAT must exist to host veridex-arena and WHO owns each piece.
Recorded at Wave 0, **before** any app dependency. Everything here is an
inventory entry, **not** an action taken: D-0 mutates no DNS, no Coolify
state, and injects no secret values. Placeholder names are marked.

## 1. VPS

| Item | Value | Status |
|---|---|---|
| Provider | operator choice (Contabo-class) | not yet ordered |
| OS | Ubuntu 22.04 LTS | planned |
| Size (min) | 4 GB RAM / 2 vCPU / 50 GB SSD | planned |
| Access | SSH key-only; password login disabled | planned |
| Host dirs | `/srv/veridex/replay-packs/curated` (curated seed packs, ro-mounted) | planned |

## 2. Coolify project

| Item | Value | Status |
|---|---|---|
| Project name | `veridex-arena` | not yet created |
| Services | `web`, `api-runtime`, `postgres` | skeleton in `compose.coolify.yml` |
| Build roots | web: `/apps/web`; api-runtime: `/` (repo root) | recorded in runbook §4 |

## 3. DNS (placeholders — NO records created in D-0)

| Record | Points to | Attached service |
|---|---|---|
| `arena.veridex.example` (placeholder) | VPS IP | `web` |
| `api.veridex.example` (placeholder) | VPS IP | `api-runtime` |
| — | — | `postgres` gets NO domain (private network only) |

## 4. Postgres service

| Item | Value |
|---|---|
| Image | `postgres:16.6-alpine` (pinned) |
| Exposure | private network only, port 5432, no domain |
| Storage | named volume `postgres-data` |
| Backups | daily `pg_dump`, 7-day retention (runbook §10) |

## 5. Named-volume plan

| Volume | Service : mount | Purpose |
|---|---|---|
| `postgres-data` | postgres : `/var/lib/postgresql/data` | database files |
| `wal-spool` | api-runtime : `/var/lib/veridex/wal` | `WAL_DIR` durable event spool (I-4/AC-13) |
| `replay-capture` | api-runtime : `/var/lib/veridex/replay-packs/capture` | writable ReplayPack capture root (R-0b/R-2) |
| (host path, ro) | api-runtime : `/var/lib/veridex/replay-packs/curated` | curated seed packs via `REPLAY_PACK_ROOT` |

## 6. Secret / config inventory (RECORDED, NOT injected)

Names and ownership only — **no values appear anywhere in git**. Values
are injected via Coolify's secret store at D-1 (with `:?` required-var
guards in compose).

| Name | Kind | Owner service | Source |
|---|---|---|---|
| `DATABASE_URL` | secret | api-runtime | Coolify Postgres connection string |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | secret | postgres | operator-generated |
| `WAL_DIR` | config | api-runtime | `/var/lib/veridex/wal` (volume mount, I-4/AC-13) |
| `REPLAY_PACK_ROOT` | config | api-runtime | `/var/lib/veridex/replay-packs/curated` (ro mount) |
| venue/provider API keys (final list owned by I-5/D-1 runtime wiring) | secret | api-runtime | operator |
| `NEXT_PUBLIC_*` build args | config (public by definition) | web | operator, wired at D-1 |

Guards already in place at D-0:
- Root `.dockerignore` and `apps/web/.dockerignore` exclude `**/*.pem`,
  `**/*.key`, wallet/keypair material, and env files from every build
  context, before any broad `COPY`.

## 7. Secret-material relocation record (D-0 action, already executed)

The one secret file that lived in the workspace tree —
`agent-rank/privy_authorization_private.pem` (untracked) — was relocated
OUT of all repos by the lane controller, unread and unopened:

- Record: `.omc/reviews/implementation/deployment/D-0/evidence/pem-relocation-record.md`
  (workspace root, outside this worktree)
- New location: `~/.veridex-secrets/privy_authorization_private.pem`
  (dir mode 700, file mode 600)
- **Key ROTATION is a REQUIRED operator-only follow-up** in the Privy
  dashboard — not a lane action. Until rotated, treat the old key as
  potentially shared.

## 8. Explicitly NOT provisioned in D-0

- No live VPS, no Coolify project, no DNS records, no certificates.
- No secret values anywhere in this repo.
- No `Dockerfile.api` (I-5-owned; D-1 wires it).
- No running container of any kind.
