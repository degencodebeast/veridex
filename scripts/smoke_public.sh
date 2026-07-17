#!/usr/bin/env bash
# smoke_public.sh — D-1 deployment smoke subset the checkpoints run against a LIVE stack.
#
# Exercises the three things a fresh deploy must get right: liveness, deployment readiness, a real
# durable request path, and a fail-closed authorization boundary. Read-only + idempotent (the one
# write it makes is POST /demo/run, an offline deterministic demo competition).
#
# Usage:  BASE_URL=https://api.example.com ./scripts/smoke_public.sh
#         (defaults to http://localhost:8000 for the LOCAL compose stack)
#
# Exit 0 + prints SMOKE_OK when every check passes; non-zero on the first failure.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"

http_code() {
  # $1=method $2=path
  curl -s -o /dev/null -w '%{http_code}' -X "$1" "${BASE_URL}$2"
}

fail() {
  echo "SMOKE_FAIL: $1" >&2
  exit 1
}

echo "smoke: target ${BASE_URL}"

# 1) Liveness — /healthz is always 200 (never gated on auth or the DB).
code="$(http_code GET /healthz)"
[ "$code" = "200" ] || fail "GET /healthz expected 200, got $code"
echo "  ok  liveness   GET /healthz -> 200"

# 2) Deployment readiness — Postgres + AgentOS session DB + ReplayPack catalog all up.
code="$(http_code GET /readyz)"
[ "$code" = "200" ] || fail "GET /readyz expected 200 (stack not ready), got $code"
echo "  ok  readiness  GET /readyz  -> 200"

# 3) Durable request path — the offline demo competition persists a run through the store.
code="$(http_code POST /demo/run)"
[ "$code" = "200" ] || fail "POST /demo/run expected 200, got $code"
echo "  ok  durable    POST /demo/run -> 200"

# 4) Fail-closed authorization — an unauthenticated control-plane write must be REFUSED (401/403),
#    never silently accepted. Proves the auth boundary is wired on the deployed surface.
code="$(http_code POST /competitions/smoke-probe/kill-switch)"
case "$code" in
  401 | 403) echo "  ok  authz      POST kill-switch (no token) -> $code (refused)" ;;
  *) fail "unauthenticated control-plane write must be 401/403, got $code" ;;
esac

echo "SMOKE_OK"
