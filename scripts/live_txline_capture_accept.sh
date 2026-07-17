#!/usr/bin/env bash
# R-0a / R-0b — TxLINE live-ingestion capture ACCEPTANCE gate. DUAL-MODE.
#
#   (default / --offline)  R-0a / CI: recording-fakes, NO network. Runs the capture chain on canned
#                          auth + canned /odds/stream frames -> a TEST pack, and asserts (a) the pack
#                          is a TEST pack (provenance is "test-fake-recording", NEVER genuine) and
#                          (b) the injected sentinel secret leaks into no artifact/log.
#   --live                 R-0b / operator-run: resolves REAL creds FAIL-CLOSED via require_live_creds
#                          (JWT + TXLINE_X_API_TOKEN), then captures a GENUINE pack from the deployed
#                          feed. That live pack — not the fake one — gates HACKATHON_QA_READY.
#
# Credentials are NEVER echoed here: creds are sourced into the environment (not printed), and the
# Python entry scrubs every diagnostic of the raw secret values.
#
# Usage:
#   scripts/live_txline_capture_accept.sh            # offline recording-fakes (CI)
#   scripts/live_txline_capture_accept.sh --offline  # same, explicit
#   scripts/live_txline_capture_accept.sh --live     # R-0b, needs real creds in env / veridex/.env
set -euo pipefail

MODE="offline"
case "${1:-}" in
  --live) MODE="live" ;;
  --offline|"") MODE="offline" ;;
  *) echo "usage: $0 [--offline|--live]" >&2; exit 2 ;;
esac

cd "$(dirname "$0")/.."   # -> veridex-arena repo root

if [ "$MODE" = "live" ]; then
  # LIVE branch (R-0b). Creds come from the operator env / veridex/.env — sourced, never echoed.
  # require_live_creds (invoked inside the Python entry) FAILS CLOSED if JWT / TXLINE_X_API_TOKEN
  # are absent; this branch never weakens that guard.
  set -a; [ -f veridex/.env ] && . veridex/.env; set +a
  exec uv run --extra api --extra live python scripts/txline_live_capture_accept.py --live
else
  # CI branch (R-0a): recording-fakes, no network, produces a TEST pack; asserts honesty + scrub.
  exec uv run --extra api --extra live python scripts/txline_live_capture_accept.py --offline
fi
