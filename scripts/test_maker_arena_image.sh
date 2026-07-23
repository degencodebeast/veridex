#!/usr/bin/env bash
# Image-level acceptance for GET /maker/arena-result (spec §8 deployment/packaging gate).
# Builds Dockerfile.api, runs it mocks-off, and asserts 200 + schema + real values + additive
# fixture_metadata + raw IDs + NO mock fallback. It ALSO proves the owner-scoped /maker/live-ab
# SUCCESS path in-image: it deploys a real maker instance through the production deploy route (dev
# principal), waits for it to SEAL, and asserts GET /maker/live-ab/{that_id} -> 200 with the guard
# ON/OFF ablation envelope — an unknown-instance 404 is only a SECONDARY negative. A direct in-image
# builder call is an intermediate gate ONLY; this running-container HTTP test is required.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="veridex-maker-acceptance:local"
CONTAINER="veridex-maker-acceptance"
PORT="${PORT:-18099}"

# F1 — the seeded Official-Replay-League running-container flow shares the SAME built $IMAGE but runs a
# SECOND API container on the Task-0-owned veridex-net wired to the shared veridex-pg Postgres, plus a
# SEPARATE seed process. F1 owns teardown of ONLY these (the two containers + the built image); Task 0
# owns creation + teardown of veridex-net / veridex-pg (left running here).
F1_NET="veridex-net"
F1_PG="veridex-pg"
F1_PG_DSN_INTERNAL="postgresql://postgres:dev@${F1_PG}:5432/postgres"
F1_CONTAINER="veridex-official-league-acceptance"
F1_SEED_CONTAINER="veridex-official-league-seed"
F1_PORT="${F1_PORT:-18098}"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  # F1: tear down ONLY what F1 created — the seeded-flow API container, the seed container, the image.
  # Leave the Task-0-owned veridex-net / veridex-pg untouched (Task 0 tears those down).
  docker rm -f "$F1_CONTAINER" >/dev/null 2>&1 || true
  docker rm -f "$F1_SEED_CONTAINER" >/dev/null 2>&1 || true
  docker rmi -f "$IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== building $IMAGE =="
docker build -f "$ROOT/Dockerfile.api" -t "$IMAGE" "$ROOT"

# INTERMEDIATE GATE: confirm the sealed paths resolve INSIDE the image (spec §13 open decision).
echo "== in-image path resolution =="
docker run --rm --entrypoint python "$IMAGE" -c \
  "from veridex.api.maker_router import SEALED_RESULT_PATH, SEALED_FIXTURES_PATH; \
p1=SEALED_RESULT_PATH; p2=SEALED_FIXTURES_PATH; print(p1); print(p2); \
assert p1.is_file(), f'sealed result missing at {p1}'; \
assert p2.is_file(), f'sealed fixtures missing at {p2}'"

echo "== running container mocks-off =="
# APP_ENV=development keeps the AUTH_MODE=dev bypass permitted (Settings HARD-REFUSES dev under a
# production APP_ENV); dev auth yields a fixed principal (did:privy:dev) with NO token, so the deploy +
# owner-scoped live-ab read below run genuinely owner-scoped through the SAME code path production uses.
# No DATABASE_URL -> the server selects an in-process InMemoryStore (explicit local-dev, not a silent
# downgrade), which persists the deployed instance for the live-ab read in the same process.
docker run -d --name "$CONTAINER" \
  -e APP_ENV=development -e AUTH_MODE=dev -e CORS_ORIGINS='http://localhost:3000' \
  -p "${PORT}:8000" "$IMAGE"

# Wait for liveness.
for _ in $(seq 1 30); do
  if curl -fsS "http://localhost:${PORT}/healthz" >/dev/null 2>&1; then break; fi
  sleep 1
done

echo "== GET /maker/arena-result =="
CODE="$(curl -s -o /tmp/maker_body.json -w '%{http_code}' "http://localhost:${PORT}/maker/arena-result")"
[ "$CODE" = "200" ] || { echo "FAIL: expected 200, got $CODE"; cat /tmp/maker_body.json; exit 1; }

python3 - <<'PY'
import json
b = json.load(open("/tmp/maker_body.json"))
assert b["schema_version"] == "maker_arena_result.v1", b["schema_version"]
assert b["lane"] == "maker" and b["source_mode"] == "replay"
r = b["result"]
assert r["fixture_universe_n"] == 18 and r["small_n_flag"] is True
assert r["real_executable_edge_bps"] is None
board = {row["agent_id"]: row for row in r["maker_leaderboard"]}
assert board["naive-mm"]["avg_toxicity_loss_bps"] == 172
assert board["txline-fair-mm"]["avg_toxicity_loss_bps"] == 129
f = r["falsification"]
assert f["delta_bps"] == 43 and f["ci_low_bps"] == 34 and f["ci_high_bps"] == 52
assert f["verdict"] == "SEPARATED"
# Raw IDs preserved (18 ints) + additive fixture_metadata (18 captured rows).
assert len(r["fixtures"]) == 18 and all(isinstance(x, int) for x in r["fixtures"])
meta = b["fixture_metadata"]
assert len(meta) == 18
assert [m["fixture_id"] for m in meta] == r["fixtures"]
assert all(m["label_source"] == "captured" for m in meta)
# NO mock fallback: the exact sealed values asserted above (172/129 bps, delta 43 CI[34,52]
# SEPARATED, n=18) ARE the proof — no mock produces them, and the backend route has no mock
# fallback (it 404s when the sealed artifact is absent rather than serving fabricated data).
print("arena-result OK")
PY

echo "== seed an OWNED maker instance via the production deploy route (dev principal) =="
# The canonical Studio MM payload deploys the quoteguard-mm family on the REAL recorded 18209181 tape
# (shipped in the curated pack COPYed into the image). The default (production) DeployDeps use the
# offline OfflineRecordingProposer, so the run seals with ZERO external I/O. No Idempotency-Key header
# is required (mirrors tests/test_maker_live_ab.py::_deploy_sealed_mm).
DEPLOY_CODE="$(curl -s -o /tmp/maker_deploy.json -w '%{http_code}' \
  -H 'Content-Type: application/json' \
  --data @"$ROOT/contracts/fixtures/studio_mm_deploy_payload.json" \
  "http://localhost:${PORT}/agents/deploy")"
[ "$DEPLOY_CODE" = "200" ] || { echo "FAIL: deploy expected 200, got $DEPLOY_CODE"; cat /tmp/maker_deploy.json; exit 1; }
INSTANCE_ID="$(python3 -c 'import json; print(json.load(open("/tmp/maker_deploy.json"))["instance_id"])')"
[ -n "$INSTANCE_ID" ] || { echo "FAIL: no instance_id in deploy response"; cat /tmp/maker_deploy.json; exit 1; }
echo "   deployed instance_id=$INSTANCE_ID — waiting for SEALED"

# Poll the owner-scoped status until the background deploy task seals (or fails). status is the DURABLE
# DeployStatus value ("pending"|"running"|"sealed"|"failed"), lower-cased on the wire.
SEALED=""
for _ in $(seq 1 60); do
  curl -s -o /tmp/maker_status.json "http://localhost:${PORT}/agents/instances/${INSTANCE_ID}/status" || true
  ST="$(python3 -c 'import json; print(json.load(open("/tmp/maker_status.json")).get("status",""))' 2>/dev/null || echo '')"
  case "$ST" in
    sealed) SEALED=1; break;;
    failed) echo "FAIL: deploy reached FAILED"; cat /tmp/maker_status.json; exit 1;;
  esac
  sleep 1
done
[ -n "$SEALED" ] || { echo "FAIL: instance did not seal in time"; cat /tmp/maker_status.json; exit 1; }

echo "== PRIMARY regression: GET /maker/live-ab/{owned instance} -> 200 (guard ON/OFF ablation) =="
LAB_CODE="$(curl -s -o /tmp/maker_liveab.json -w '%{http_code}' "http://localhost:${PORT}/maker/live-ab/${INSTANCE_ID}")"
[ "$LAB_CODE" = "200" ] || { echo "FAIL: live-ab expected 200 for the owned maker instance, got $LAB_CODE"; cat /tmp/maker_liveab.json; exit 1; }
python3 - <<'PY'
import json
b = json.load(open("/tmp/maker_liveab.json"))
assert b["schema_version"] == "maker_live_ab.v1", b["schema_version"]
assert b["lane"] == "maker" and b["panel"] == "guard_on_off_ablation"
assert b["is_ablation"] is True
assert b["mode"] == "replay"  # FORCED non-executing replay (never the reconstructed replay_dry_run)
# NO mock fallback: the frontend mock live-ab identity must never leak into this real owned-instance
# body — this is where a mock-ablation fallback would actually surface if one existed.
assert "mm-inst-0f74a4" not in json.dumps(b), "mock live-ab identity leaked into real live-ab body"
# Both arms folded the real recorded tape and produced a matched decision stream …
assert len(b["guard_off"]["decisions"]) >= 1, "guard_off produced no decisions"
assert len(b["guard_on"]["decisions"]) >= 1, "guard_on produced no decisions"
assert len(b["guard_off"]["decisions"]) == len(b["guard_on"]["decisions"])
assert isinstance(b["divergent_frame_indices"], list) and isinstance(b["diverges"], bool)
# … under the ablation-not-ranking labels, with NO rank/PnL/fill/edge field anywhere in the envelope.
assert "panel_disclaimer" in b["labels"]
def all_keys(v):
    ks = set()
    if isinstance(v, dict):
        for k, sub in v.items():
            ks.add(k); ks |= all_keys(sub)
    elif isinstance(v, list):
        for sub in v: ks |= all_keys(sub)
    return ks
banned = ("rank", "toxicity", "pnl", "fill", "edge", "order_id", "venue")
for k in all_keys(b):
    assert not any(tok in k.lower() for tok in banned), f"forbidden token in key: {k}"
print("live-ab (owned instance) OK")
PY

echo "== SECONDARY negative: unknown instance -> 404 (never 500) =="
CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}/maker/live-ab/does-not-exist")"
case "$CODE" in
  404) echo "live-ab unknown-instance OK ($CODE)";;
  *) echo "FAIL: live-ab unknown-instance expected 404, got $CODE"; exit 1;;
esac

# ======================================================================================================
# F1 — SEEDED Official Replay League: the REAL running-container proof over the SHARED durable Postgres.
# Builds on the SAME $IMAGE (already built above): runs a SECOND API container on the Task-0 veridex-net
# wired to veridex-pg, drives the seed as a SEPARATE process against the SAME Postgres, then HTTP-asserts
# the four league surfaces over the container (roster / official board / replay markets / sealed maker).
# ======================================================================================================

echo "== F1 (a) readiness: Task-0 veridex-net + veridex-pg must exist (REUSED, never recreated) =="
docker network inspect "$F1_NET" >/dev/null 2>&1 || { echo "FAIL: network $F1_NET absent (Task 0 owns it)"; exit 1; }
docker exec "$F1_PG" pg_isready >/dev/null 2>&1 || { echo "FAIL: $F1_PG not accepting connections (Task 0 owns it)"; exit 1; }
echo "   $F1_NET + $F1_PG present and ready"

echo "== F1 (a) compose-config assertion: the deployed topology catalogs as demo_pack_real =="
# The standalone Dockerfile bake is asserted implicitly by the seed passing below; this exercises the
# COMPOSE override path (compose.coolify.yml) that the bare `docker run` does NOT. The mount TARGET leaf
# is what the in-container catalog derives pack_id from, and it must be demo_pack_real (never curated).
CFG="$(DATABASE_URL=x CORS_ORIGINS=http://localhost OPERATOR_TOKEN=x PRIVY_APP_ID=x PRIVY_VERIFICATION_KEY=x \
  POSTGRES_USER=postgres POSTGRES_PASSWORD=dev POSTGRES_DB=postgres \
  NEXT_PUBLIC_API_BASE=http://localhost:8000 NEXT_PUBLIC_PRIVY_APP_ID=x \
  docker compose -f "$ROOT/compose.coolify.yml" config 2>/dev/null)"
echo "$CFG" | grep -q '/var/lib/veridex/replay-packs/demo_pack_real' \
  || { echo "FAIL: compose config missing the demo_pack_real seed leaf"; exit 1; }
echo "$CFG" | grep -q '/replay-packs/curated' \
  && { echo "FAIL: compose config still references the stale 'curated' leaf"; exit 1; }
echo "   compose config catalogs the demo_pack_real leaf (no stale 'curated')"

echo "== F1 (a) RESET the shared Postgres to a clean slate (deterministic 2-row assertions) =="
# Drop + recreate public BEFORE the API container starts, so the container's startup init_db rebuilds the
# tables fresh. Run inside veridex-pg so no host psql client is required.
docker exec "$F1_PG" psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;' >/dev/null \
  || { echo "FAIL: could not reset the shared Postgres public schema"; exit 1; }
echo "   public schema reset"

echo "== F1 (b) run the API container on $F1_NET wired to $F1_PG (durable Postgres, fail-closed CORS) =="
# APP_ENV=development + AUTH_MODE=dev => the seed's fixed dev principal (no Privy material needed).
# CORS_ORIGINS is required (create_server_app fails closed without it). REPLAY_PACK_ROOT points at the
# BAKED pack (leaf demo_pack_real => pack_id="demo_pack_real"), matching the seed's phase-1 assert_pack.
docker run -d --name "$F1_CONTAINER" --network "$F1_NET" \
  -e APP_ENV=development -e AUTH_MODE=dev \
  -e DATABASE_URL="$F1_PG_DSN_INTERNAL" \
  -e CORS_ORIGINS='http://localhost' \
  -e REPLAY_PACK_ROOT=/opt/veridex/replay-packs/demo_pack_real \
  -p "${F1_PORT}:8000" "$IMAGE" >/dev/null

# Wait for deployment READINESS (durable Postgres + catalog), deeper than /healthz liveness.
F1_READY=""
for _ in $(seq 1 60); do
  if curl -fsS "http://localhost:${F1_PORT}/readyz" >/dev/null 2>&1; then F1_READY=1; break; fi
  sleep 1
done
[ -n "$F1_READY" ] || { echo "FAIL: F1 API container did not become ready"; docker logs "$F1_CONTAINER" 2>&1 | tail -30; exit 1; }
echo "   F1 API container ready on :${F1_PORT}"

echo "== F1 (d) AC-11 pre-seed capture: the sealed maker result.fixtures BEFORE the directional seed =="
curl -fsS "http://localhost:${F1_PORT}/maker/arena-result" -o /tmp/f1_maker_before.json \
  || { echo "FAIL: pre-seed /maker/arena-result did not return 200"; exit 1; }

echo "== F1 (c) seed the Official Replay League as a SEPARATE process writing the SAME Postgres =="
docker run --rm --name "$F1_SEED_CONTAINER" --network "$F1_NET" \
  -e APP_ENV=development -e AUTH_MODE=dev \
  -e DATABASE_URL="$F1_PG_DSN_INTERNAL" \
  -e CORS_ORIGINS='http://localhost' \
  -e REPLAY_PACK_ROOT=/opt/veridex/replay-packs/demo_pack_real \
  "$IMAGE" python /opt/veridex/scripts/seed_official_replay_league.py --seed-revision r1 \
  || { echo "FAIL: seed process exited non-zero"; exit 1; }

echo "== F1 (d) POLL GET /agents/roster until BOTH officials are present (deploys seal async) =="
ROSTER_READY=""
for _ in $(seq 1 60); do
  curl -fsS "http://localhost:${F1_PORT}/agents/roster" -o /tmp/f1_roster.json 2>/dev/null || true
  N="$(python3 -c 'import json;print(len(json.load(open("/tmp/f1_roster.json"))["agents"]))' 2>/dev/null || echo 0)"
  if [ "$N" = "2" ]; then ROSTER_READY=1; break; fi
  sleep 1
done
[ -n "$ROSTER_READY" ] || { echo "FAIL: roster never reached 2 officials"; cat /tmp/f1_roster.json 2>/dev/null; exit 1; }

echo "== F1 (d) HTTP-assert roster: 2 SEALED officials, NUMERIC avg_clv_bps, human display_name =="
python3 - <<'PY'
import json
agents = json.load(open("/tmp/f1_roster.json"))["agents"]
assert len(agents) == 2, agents
for a in agents:
    assert a["status"] == "sealed", a
    assert isinstance(a["avg_clv_bps"], (int, float)), a          # numeric pooled CLV, never a None hole
    assert a["display_name"] and a["display_name"] != a["public_agent_id"], a  # human name, not opaque id
print("   roster OK:", sorted(a["public_agent_id"] for a in agents))
PY

echo "== F1 (d) HTTP-assert /leaderboard/directional?board_kind=official_benchmark: 2 pooled rows =="
curl -fsS "http://localhost:${F1_PORT}/leaderboard/directional?board_kind=official_benchmark" -o /tmp/f1_board.json \
  || { echo "FAIL: official board did not return 200"; exit 1; }
python3 - <<'PY'
import json
b = json.load(open("/tmp/f1_board.json"))
assert b["board_kind"] == "official_benchmark", b
rows = b["rows"]
# ASSERT THE OFFICIAL BOARD TOPOLOGY (exactly 2 officials), not a global competition count.
assert len(rows) == 2, rows
for r in rows:
    assert r["avg_clv_bps"] is not None, r
    assert r["source_mode"] == "all-replay", r
print("   official board OK: 2 pooled all-replay rows")
PY

echo "== F1 (d) HTTP-assert replay markets 18213979: 30 markets, 13 suspended, CAPTURED REPLAY =="
curl -fsS "http://localhost:${F1_PORT}/replay-packs/demo_pack_real/fixtures/18213979/markets" -o /tmp/f1_markets.json \
  || { echo "FAIL: replay markets did not return 200 (pack_id must be demo_pack_real)"; exit 1; }
python3 - <<'PY'
import json
b = json.load(open("/tmp/f1_markets.json"))
assert b["label"] == "CAPTURED REPLAY", b["label"]
markets = b["markets"]
assert len(markets) == 30, len(markets)
suspended = [m for m in markets if m["suspended"]]
assert len(suspended) == 13, len(suspended)
for m in suspended:
    assert m["stable_prob_bps"] == {}, m       # HONESTY: empty prob map, never back-filled
    assert m["stable_price"], m                # retained last-known price
print("   replay markets OK: 30 markets / 13 suspended / CAPTURED REPLAY")
PY

echo "== F1 (d) AC-11 HTTP-assert: sealed maker result.fixtures byte-UNCHANGED across the seed =="
curl -fsS "http://localhost:${F1_PORT}/maker/arena-result" -o /tmp/f1_maker_after.json \
  || { echo "FAIL: post-seed /maker/arena-result did not return 200"; exit 1; }
python3 - <<'PY'
import json
before = json.load(open("/tmp/f1_maker_before.json"))["result"]["fixtures"]
after = json.load(open("/tmp/f1_maker_after.json"))["result"]["fixtures"]
assert json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True), (before, after)
print("   sealed maker result.fixtures UNCHANGED (AC-11): seed did not perturb Maker evidence")
PY

echo "ALL F1 SEEDED-LEAGUE CONTAINER CHECKS PASSED"

echo "ALL IMAGE CHECKS PASSED"
