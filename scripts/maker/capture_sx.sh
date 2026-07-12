#!/usr/bin/env bash
# SX Bet + TxLINE WS capture with tee'd stdout logging (the missing piece from the Norway-England run).
# Captures BOTH the JSONL tape AND the poller's stdout (WS banner + gap_count/reconnect logs), so a later
# lead-lag analysis can distinguish WS reconnects from bookmaker suspensions.
#
# Usage: scripts/maker/capture_sx.sh <fixture_id> <name> [duration_s] [poll_interval_s]
#   e.g. scripts/maker/capture_sx.sh 18222446 arg-sui            # Argentina-Switzerland, defaults (WS, 0.5s, 9000s)
#        scripts/maker/capture_sx.sh 18213979 nor-eng 9000 0.5
set -euo pipefail
FIXTURE_ID="${1:?usage: capture_sx.sh <fixture_id> <name> [duration_s] [poll_interval_s]}"
NAME="${2:?usage: capture_sx.sh <fixture_id> <name> [duration_s] [poll_interval_s]}"
DURATION="${3:-9000}"
POLL="${4:-0.5}"

cd "$(dirname "$0")/../.."          # -> veridex-arena repo root
# Load creds (JWT + TXLINE_X_API_TOKEN + SX_BET_API_KEY) from veridex/.env; never echoed.
set -a; source veridex/.env; set +a

mkdir -p captures/sx-leadlag
OUT="captures/sx-leadlag/${NAME}.jsonl"
LOG="captures/sx-leadlag/${NAME}.stdout.log"
echo "[capture_sx] fixture=${FIXTURE_ID} name=${NAME} out=${OUT} log=${LOG} path=WS poll=${POLL}s duration=${DURATION}s"
echo "[capture_sx] watch:  tail -f ${LOG}     (stdout: WS banner + gaps)"
echo "[capture_sx] watch:  tail -f ${OUT}     (tape rows)"

# 2>&1 | tee: stdout+stderr go to the log AND the terminal; pipefail propagates the poller's exit code.
./.venv/bin/python -m scripts.maker.sx_bet_poller \
  --fixtures scripts/txline_live/wc-qf-fixtures.json \
  --fixture-id "${FIXTURE_ID}" \
  --out "${OUT}" \
  --use-ws \
  --poll-interval-s "${POLL}" \
  --duration-s "${DURATION}" \
  2>&1 | tee "${LOG}"
