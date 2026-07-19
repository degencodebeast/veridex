#!/usr/bin/env bash
# smoke_public.sh — III-1 public deployment hardening: operator-run acceptance instrument.
#
# Hardens the D-1/II-11 deploy (does NOT redeploy anything). Verifies a DEPLOYED (or
# locally-booted) veridex-arena stack end-to-end:
#   1. liveness            GET  /healthz            — router.py:424-427, always 200
#   2. readiness            GET  /readyz             — readiness.py:361-377, durable-deps gate
#   3. deploy-auth (fail-closed)
#                            POST /competitions/{id}/kill-switch — router.py:1654-1684
#                            The `_require_operator` bearer-token dependency runs BEFORE the
#                            competition lookup, so an unauthenticated call is ALWAYS 401 —
#                            regardless of whether `{id}` exists (see router.py:1657, 1680-1684).
#                            (POST /competitions is intentionally NOT used here: it gates on
#                            Privy `require_principal`, which auto-issues a principal with NO
#                            token at all when `AUTH_MODE=dev` — auth_privy.py:117-118 — so it
#                            would silently PASS on a misconfigured dev-mode deploy instead of
#                            proving the boundary.)
#   4. replay run reachable (judge cold-hit path, no operator help)
#                            POST /demo/run  — router.py:520-559, unauthenticated, persists a
#                            source_mode="replay" run through the durable store
#                            GET  /leaderboard — router.py:563-594, asserts the run is visible
#
# AC-13 restart-durability (separate BINARY GATE — run explicitly, not part of the default pass):
#   `GET /runs/{run_id}` (router.py:694-735) loads straight from the durable store
#   (`dep_store.load_run`), independent of the in-process `_run_meta` registry that
#   `/leaderboard` depends on (router.py:431) — `_run_meta` does NOT survive a restart even if
#   the underlying Postgres-backed store does, so it is the wrong probe for AC-13. `GET
#   /runs/{run_id}` is the right one: if the SAME run_id it returned before a restart still
#   returns 200 after, the WAL -> Postgres replay kept the durable run store intact.
#
#   Usage (restart is infra-specific — an OPERATOR step unless RESTART_CMD is given):
#     ./scripts/smoke_public.sh --ac13-pre                 # records a run_id, saves it locally
#     <operator restarts the api-runtime container>         # e.g. `docker compose restart api-runtime`
#     ./scripts/smoke_public.sh --ac13-post                # re-queries the SAME run_id, asserts survival
#   Or, single-shot automated (when a restart command is available in-process):
#     RESTART_CMD='docker compose restart api-runtime' ./scripts/smoke_public.sh --ac13
#
# Usage (default smoke):
#   BASE_URL=https://api.example.com ./scripts/smoke_public.sh
#   (defaults to http://localhost:8000 — the LOCAL compose stack's API_HOST_PORT; see
#   compose.coolify.yml:77, .env.example:71, README.md:250-252)
#
# Fail-closed: an empty BASE_URL or an unreachable host aborts loudly (non-zero) before any
# checks run. Never reads or prints a bearer token — every request here is deliberately
# UNAUTHENTICATED (that is what checks 3 and AC-13 are proving).
set -uo pipefail

BASE_URL="${BASE_URL:-}"
MODE=""
for arg in "$@"; do
  case "$arg" in
    --ac13|--ac13-pre|--ac13-post|-h|--help) MODE="$arg" ;;
    *) BASE_URL="${BASE_URL:-$arg}" ;;
  esac
done
BASE_URL="${BASE_URL:-http://localhost:8000}"

CURL_TIMEOUT=15
BODY_FILE="$(mktemp)"
AC13_STATE_FILE="${AC13_STATE_FILE:-${TMPDIR:-/tmp}/veridex_smoke_ac13_state}"
trap 'rm -f "$BODY_FILE"' EXIT

FAILURES=0

usage() {
  cat <<'EOF'
Usage: smoke_public.sh [BASE_URL] [--ac13 | --ac13-pre | --ac13-post]
  (no flag)    run the default smoke: liveness, readiness, deploy-auth, replay-visible
  --ac13-pre   AC-13 step 1: record a durable run_id (operator restarts the container next)
  --ac13-post  AC-13 step 2: verify the SAME run_id survived the restart
  --ac13       AC-13 single-shot: requires RESTART_CMD env (e.g. 'docker compose restart api-runtime')

Env: BASE_URL (default http://localhost:8000), AC13_STATE_FILE, RESTART_CMD
EOF
}

abort() {
  echo "SMOKE ABORT: $1" >&2
  exit 2
}

pass() { printf '  PASS  %-10s %s\n' "$1" "$2"; }
fail() { printf '  FAIL  %-10s %s\n' "$1" "$2" >&2; FAILURES=$((FAILURES + 1)); }

require_jq() {
  command -v jq >/dev/null 2>&1 || abort "jq is required to parse JSON responses (install jq)"
}

# req METHOD PATH — sets HTTP_CODE, leaves the response body in $BODY_FILE. Returns non-zero (and
# leaves HTTP_CODE unset) ONLY on a transport-level failure (unreachable host, DNS, timeout) — an
# HTTP error status (4xx/5xx) is a normal, successful request and returns 0.
req() {
  local method="$1" path="$2"
  if ! HTTP_CODE="$(curl -sS --max-time "$CURL_TIMEOUT" -o "$BODY_FILE" -w '%{http_code}' -X "$method" "${BASE_URL}${path}" 2>&1)"; then
    return 1
  fi
}

[ -n "$BASE_URL" ] || abort "BASE_URL is empty"

if [ "$MODE" = "-h" ] || [ "$MODE" = "--help" ]; then
  usage
  exit 0
fi

echo "smoke: target ${BASE_URL}"

# Preflight: also check 1 (liveness). A transport failure here means the host is unreachable —
# loud abort, distinct from a per-check FAIL, since nothing downstream can be trusted either.
if ! req GET /healthz; then
  abort "cannot reach ${BASE_URL} (${HTTP_CODE:-transport error}) — is the stack up / is BASE_URL correct?"
fi
if [ "$HTTP_CODE" = "200" ]; then
  pass liveness "GET /healthz -> 200"
else
  fail liveness "GET /healthz expected 200, got $HTTP_CODE"
fi

# --- AC-13 modes (separate from the default 4-check smoke) ------------------------------------

ac13_pre() {
  require_jq
  req POST /demo/run || abort "cannot reach ${BASE_URL} for AC-13 pre-step"
  [ "$HTTP_CODE" = "200" ] || abort "AC-13 pre: POST /demo/run expected 200, got $HTTP_CODE"
  local run_id
  run_id="$(jq -r '.run_id' "$BODY_FILE")"
  [ -n "$run_id" ] && [ "$run_id" != "null" ] || abort "AC-13 pre: POST /demo/run returned no run_id"

  req GET "/runs/${run_id}" || abort "cannot reach ${BASE_URL} for AC-13 pre-step verification"
  [ "$HTTP_CODE" = "200" ] || abort "AC-13 pre: GET /runs/${run_id} expected 200 immediately after creation, got $HTTP_CODE"

  printf '%s\n%s\n' "$BASE_URL" "$run_id" > "$AC13_STATE_FILE"
  echo "AC13_PRE_OK run_id=${run_id}"
  echo "  -> now restart the api-runtime container, then run: $0 --ac13-post"
}

ac13_post() {
  [ -f "$AC13_STATE_FILE" ] || abort "no AC-13 state at ${AC13_STATE_FILE} — run --ac13-pre first"
  local saved_base saved_run_id
  saved_base="$(sed -n '1p' "$AC13_STATE_FILE")"
  saved_run_id="$(sed -n '2p' "$AC13_STATE_FILE")"
  [ -n "$saved_run_id" ] || abort "AC-13 state file is malformed (no run_id)"
  if [ "$saved_base" != "$BASE_URL" ]; then
    echo "  note: --ac13-pre ran against ${saved_base}, querying that same target for --ac13-post" >&2
  fi

  if ! HTTP_CODE="$(curl -sS --max-time "$CURL_TIMEOUT" -o "$BODY_FILE" -w '%{http_code}' -X GET "${saved_base}/runs/${saved_run_id}" 2>&1)"; then
    echo "AC-13 UNMET: ${saved_base} unreachable after restart (${HTTP_CODE:-transport error})" >&2
    exit 1
  fi

  if [ "$HTTP_CODE" = "200" ]; then
    echo "AC13_MET run_id=${saved_run_id} — durable run store survived the restart (GET /runs/${saved_run_id} -> 200)"
    rm -f "$AC13_STATE_FILE"
    exit 0
  else
    echo "AC-13 UNMET: GET /runs/${saved_run_id} expected 200 after restart, got ${HTTP_CODE} — the durable run was LOST (check DATABASE_URL / WAL_DIR wiring, not an in-memory store)" >&2
    exit 1
  fi
}

case "$MODE" in
  --ac13-pre)
    ac13_pre
    exit 0
    ;;
  --ac13-post)
    ac13_post
    ;;
  --ac13)
    [ -n "${RESTART_CMD:-}" ] || abort "--ac13 requires RESTART_CMD (e.g. RESTART_CMD='docker compose restart api-runtime'); otherwise use --ac13-pre / --ac13-post across a manual restart"
    ac13_pre
    echo "  running RESTART_CMD: ${RESTART_CMD}"
    eval "$RESTART_CMD" || abort "RESTART_CMD failed"
    echo "  waiting for /readyz to come back..."
    ready=0
    for _ in $(seq 1 30); do
      if req GET /readyz && [ "$HTTP_CODE" = "200" ]; then
        ready=1
        break
      fi
      sleep 2
    done
    [ "$ready" = "1" ] || abort "stack did not report /readyz=200 within 60s after restart"
    ac13_post
    ;;
esac

# --- Default smoke: checks 2-4 --------------------------------------------------------------

require_jq

# 2) Readiness — Postgres + runtime-event/OPS spool + ReplayPack catalog (readiness.py:118-126).
req GET /readyz || abort "cannot reach ${BASE_URL} for /readyz"
echo "  readyz body: $(cat "$BODY_FILE")"
if [ "$HTTP_CODE" = "200" ] && [ "$(jq -r '.ready' "$BODY_FILE")" = "true" ]; then
  pass readiness "GET /readyz -> 200, ready=true"
else
  ready_val="$(jq -r 'if has("ready") then (.ready | tostring) else "?" end' "$BODY_FILE" 2>/dev/null)"
  fail readiness "GET /readyz expected 200 + ready:true, got HTTP $HTTP_CODE, ready=${ready_val:-?}"
fi

# 3) Deploy-auth fail-closed — unauthenticated control-plane write must be refused with 401
#    (router.py:1654-1684: the operator-bearer-token dependency runs before any competition
#    lookup, so this is 401 even though "smoke-probe" is not a real competition id).
req POST /competitions/smoke-probe/kill-switch
if [ "$HTTP_CODE" = "401" ]; then
  pass deploy-auth "POST /competitions/{id}/kill-switch (no token) -> 401"
else
  fail deploy-auth "expected 401 (unauthenticated control-plane write must be refused), got $HTTP_CODE"
fi

# 4) Replay run reachable — the judge cold-hit path: an unauthenticated replay-mode run is
#    creatable and visible on the leaderboard with no operator setup (router.py:520-594).
req POST /demo/run
if [ "$HTTP_CODE" = "200" ]; then
  run_id="$(jq -r '.run_id' "$BODY_FILE" 2>/dev/null)"
  req GET /leaderboard
  rows_len="$(jq -r '.rows | length' "$BODY_FILE" 2>/dev/null || echo 0)"
  if [ "$HTTP_CODE" = "200" ] && [ "${rows_len:-0}" -gt 0 ] 2>/dev/null; then
    pass replay-run "POST /demo/run -> 200 (run_id=${run_id}), GET /leaderboard -> 200 (${rows_len} row(s))"
  else
    fail replay-run "GET /leaderboard expected 200 + non-empty rows, got HTTP $HTTP_CODE, rows=${rows_len:-0}"
  fi
else
  fail replay-run "POST /demo/run expected 200, got $HTTP_CODE"
fi

echo "---"
if [ "$FAILURES" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
  exit 0
else
  echo "SMOKE FAILED (${FAILURES} checks)"
  exit 1
fi
