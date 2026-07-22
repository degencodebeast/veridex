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

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
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

echo "ALL IMAGE CHECKS PASSED"
