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
#   5. public WebSocket      WS /competitions/{id}/arena — when WS_COMPETITION_ID identifies a
#                            finalized competition with at least two canonical events, proves the
#                            reverse-proxy Upgrade plus disconnect/reconnect from since_seq with an
#                            exact, duplicate-free missing tail. Without that operator-provided id,
#                            reports OPERATOR_ACCEPTANCE_PENDING rather than claiming acceptance.
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
#   Trust boundary: AC13_MET is only as strong as RESTART_CMD (or the operator's manual step)
#   actually restarting the service — a no-op RESTART_CMD reports MET without a real restart.
#   This script cannot independently verify the restart happened (no process-uptime endpoint
#   exists to probe); prefer --ac13-pre / --ac13-post around a restart you have verified yourself.
#
# Usage (default smoke):
#   BASE_URL=https://api.example.com WS_COMPETITION_ID=c_... ./scripts/smoke_public.sh
#   (defaults to http://localhost:8000 — the LOCAL compose stack's API_HOST_PORT; see
#   compose.coolify.yml:77, .env.example:71, README.md:250-252)
#
# Fail-closed: an unreachable host aborts loudly (non-zero) before any checks run. BASE_URL always
# has a value — it defaults to http://localhost:8000 rather than requiring one. Never reads or
# prints a bearer token — every request here is deliberately UNAUTHENTICATED (that is what checks
# 3 and AC-13 are proving). BASE_URL values containing embedded credentials are refused before the
# target is printed.
set -uo pipefail

usage() {
  cat <<'EOF'
Usage: smoke_public.sh [BASE_URL] [--ac13 | --ac13-pre | --ac13-post]
  (no flag)    run liveness, readiness, deploy-auth, replay-visible, and configured WebSocket smoke
  --ac13-pre   AC-13 step 1: record a durable run_id (operator restarts the container next)
  --ac13-post  AC-13 step 2: verify the SAME run_id survived the restart
  --ac13       AC-13 single-shot: requires RESTART_CMD env (e.g. 'docker compose restart api-runtime')

Env: BASE_URL (default http://localhost:8000), WS_COMPETITION_ID (enables WebSocket acceptance),
     WS_TIMEOUT_SECONDS (default 15), PYTHON_BIN (default python3), AC13_STATE_FILE, RESTART_CMD
EOF
}

BASE_URL="${BASE_URL:-}"
MODE=""
for arg in "$@"; do
  case "$arg" in
    --ac13|--ac13-pre|--ac13-post|-h|--help) MODE="$arg" ;;
    --*)
      echo "unrecognized flag: $arg" >&2
      usage >&2
      exit 2
      ;;
    *) BASE_URL="${BASE_URL:-$arg}" ;;
  esac
done
BASE_URL="${BASE_URL:-http://localhost:8000}"
case "$BASE_URL" in
  *://*@*) abort_message="BASE_URL must not contain embedded credentials" ;;
  *) abort_message="" ;;
esac

CURL_TIMEOUT=15
BODY_FILE="$(mktemp)"
AC13_STATE_FILE="${AC13_STATE_FILE:-${TMPDIR:-/tmp}/veridex_smoke_ac13_state}"
trap 'rm -f "$BODY_FILE"' EXIT

FAILURES=0

abort() {
  echo "SMOKE ABORT: $1" >&2
  exit 2
}

[ -z "$abort_message" ] || abort "$abort_message"

pass() { printf '  PASS  %-10s %s\n' "$1" "$2"; }
fail() { printf '  FAIL  %-10s %s\n' "$1" "$2" >&2; FAILURES=$((FAILURES + 1)); }

require_jq() {
  command -v jq >/dev/null 2>&1 || abort "jq is required to parse JSON responses (install jq)"
}

require_curl() {
  command -v curl >/dev/null 2>&1 || abort "curl is required to run this smoke script"
}

# req METHOD PATH — sets HTTP_CODE, leaves the response body in $BODY_FILE. Returns non-zero on a
# transport-level failure (unreachable host, DNS, timeout) — in that case HTTP_CODE is NOT cleanly
# unset, it holds curl's mixed stdout/stderr diagnostic text, so callers must branch on the return
# code (`req ... || abort ...`), never on HTTP_CODE's contents after a failed call. An HTTP error
# status (4xx/5xx) is a normal, successful request and returns 0.
req() {
  local method="$1" path="$2"
  if ! HTTP_CODE="$(curl -sS --max-time "$CURL_TIMEOUT" -o "$BODY_FILE" -w '%{http_code}' -X "$method" "${BASE_URL}${path}" 2>&1)"; then
    return 1
  fi
}

if [ "$MODE" = "-h" ] || [ "$MODE" = "--help" ]; then
  usage
  exit 0
fi

require_curl

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
req POST /competitions/smoke-probe/kill-switch || abort "cannot reach ${BASE_URL} for deploy-auth check"
if [ "$HTTP_CODE" = "401" ]; then
  pass deploy-auth "POST /competitions/{id}/kill-switch (no token) -> 401"
else
  fail deploy-auth "expected 401 (unauthenticated control-plane write must be refused), got $HTTP_CODE"
fi

# 4) Replay run reachable — the judge cold-hit path: an unauthenticated replay-mode run is
#    creatable and visible on the leaderboard with no operator setup (router.py:520-594).
req POST /demo/run || abort "cannot reach ${BASE_URL} for replay-run check"
if [ "$HTTP_CODE" = "200" ]; then
  run_id="$(jq -r '.run_id' "$BODY_FILE" 2>/dev/null)"
  req GET /leaderboard || abort "cannot reach ${BASE_URL} for replay-run check (leaderboard)"
  rows_len="$(jq -r '.rows | length' "$BODY_FILE" 2>/dev/null || echo 0)"
  if [ "$HTTP_CODE" = "200" ] && [ "${rows_len:-0}" -gt 0 ] 2>/dev/null; then
    pass replay-run "POST /demo/run -> 200 (run_id=${run_id}), GET /leaderboard -> 200 (${rows_len} row(s))"
  else
    fail replay-run "GET /leaderboard expected 200 + non-empty rows, got HTTP $HTTP_CODE, rows=${rows_len:-0}"
  fi
else
  fail replay-run "POST /demo/run expected 200, got $HTTP_CODE"
fi

# 5) Public WebSocket Upgrade + reconnect replay. A competition id is intentionally explicit:
#    /demo/run does not create a competition-scoped canonical event log, and this smoke never uses
#    or prints production credentials to create one. CI without a deployed target reports pending.
if [ -n "${WS_COMPETITION_ID:-}" ]; then
  PYTHON_BIN="${PYTHON_BIN:-python3}"
  WS_TIMEOUT_SECONDS="${WS_TIMEOUT_SECONDS:-15}"
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || abort "${PYTHON_BIN} is required for WebSocket acceptance"
  SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  if "$PYTHON_BIN" "$SCRIPT_DIR/smoke_public_ws.py" \
    "$BASE_URL" "$WS_COMPETITION_ID" --timeout "$WS_TIMEOUT_SECONDS"; then
    pass websocket "public Upgrade + canonical reconnect tail verified"
  else
    fail websocket "public Upgrade or canonical reconnect-tail verification failed"
  fi
else
  echo "OPERATOR_ACCEPTANCE_PENDING websocket: set WS_COMPETITION_ID for a finalized competition with at least two canonical events"
fi

echo "---"
if [ "$FAILURES" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
  # Stable machine sentinel for programmatic checks (e.g. the D-1 compose-deploy test greps this).
  echo "SMOKE_OK"
  exit 0
else
  echo "SMOKE FAILED (${FAILURES} checks)"
  exit 1
fi
