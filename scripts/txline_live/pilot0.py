"""Pilot-0 — predeclared, pinned drift CLV run over 3 real WC ReplayPacks.

This script IS the pre-run stamp: the committed `EvalProtocol` below (fixtures, roster,
window, close semantics, `committed_at`) is fixed BEFORE any number exists. Committing this
file = committing the protocol. See `.omc/research/pilot-0-predeclared-protocol.md` for the
full human-readable predeclaration (framing, pinned config/pack hashes, honest baseline role).

Pilot-0 is n=3 PIPELINE VALIDATION of the D2-corrected CON-040 kickoff-close CLV path — NOT an
edge claim, NOT a beats-baselines claim. Baselines ride as null/abstention references (clv_bps
None in this producer path). Market-quality filter NOT applied (follow-up task). All `veridex`
imports are lazy inside the function (CON-010) so importing this module stays network/credential
free.

Run: .venv/bin/python scripts/txline_live/pilot0.py   (reads local packs; no network, no creds)
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# --- PINNED PROTOCOL (pre-run stamp) ---
PROTOCOL_ID = "pilot-0-drift-clv-3fx"
FIXTURE_IDS = [18179763, 18179551, 18176123]
STRATEGY_CONFIGS = ["cumulative-drift"]
BASELINES = ["no_trade", "favorite", "threshold_move", "seeded_random"]
WINDOW_ID = "pilot0-pre_match-con040"
CLOSE_SEMANTICS = "pre_match"
COMMITTED_AT = "2026-07-04T09:35:01Z"
PACKS_DIR = Path(__file__).parent / "packs"


async def run() -> dict:
    from veridex.backtest.evaluation import (  # noqa: PLC0415
        EvalProtocol,
        produce_results_by_fixture,
        run_multi_fixture_evaluation,
    )

    protocol = EvalProtocol(
        protocol_id=PROTOCOL_ID,
        fixture_ids=FIXTURE_IDS,
        strategy_configs=STRATEGY_CONFIGS,
        window=WINDOW_ID,
        close_semantics=CLOSE_SEMANTICS,
        baselines=BASELINES,
        committed_at=COMMITTED_AT,
    )
    packs = {fid: PACKS_DIR / str(fid) for fid in FIXTURE_IDS}
    for fid, path in packs.items():
        if not (path / "pack.json").exists():
            raise SystemExit(f"missing pack for fixture {fid} at {path} — run backfill.py first")

    # Pilot-0 is drift-only; no venue leg (VvV is gated behind the Polymarket leg + P1).
    results_by_fixture = await produce_results_by_fixture(protocol, packs=packs, venue_price_source=None)
    report = run_multi_fixture_evaluation(protocol, results_by_fixture=results_by_fixture, cadence_ok=False)
    return {"protocol_id": PROTOCOL_ID, "committed_at": COMMITTED_AT, "report": report, "results_by_fixture": results_by_fixture}


def main() -> None:
    out = asyncio.run(run())
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
