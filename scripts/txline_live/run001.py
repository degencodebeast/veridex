"""Run-001 — pinned, predeclared drift-vs-deterministic-baselines CLV run over 18 WC packs.

This script IS the executable pre-run stamp: the committed `EvalProtocol` below (18 fixtures,
roster, window, close semantics, filter config, `committed_at`) + the pinned eligible-universe
hash are fixed BEFORE any number exists. Committing this file = committing the invocation. See
`.omc/research/run-001-predeclared-protocol.md` for the full predeclaration and
`.omc/research/edge-validation-runs/run-001-eligible-manifest.json` for the exact eligible universe.

Market-quality filter is ON (FU-2), applied identically to drift + baselines; baselines are
LAW-SCORED (FU-3); the close is the CON-040 kickoff close (D2). NO CLI knobs — the protocol is
fixed. The run SELF-VERIFIES that its `manifest_sink` reproduces the committed universe hash; a
divergence VOIDS the run (something changed since the stamp). All `veridex` imports are lazy
inside the function (CON-010) so importing this module stays network/credential free.

Run: .venv/bin/python scripts/txline_live/run001.py   (reads local packs; no network, no creds)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# --- PINNED PROTOCOL (pre-run stamp) ---
PROTOCOL_ID = "run-001-drift-vs-baselines-18fx"
FIXTURE_IDS = [
    18179763, 18179551, 18176123, 17588229, 17588234, 17926593, 17588245, 17588391, 17588404,
    17588325, 18167317, 18172469, 18175983, 18172280, 18175981, 18179550, 18179759, 18175918,
]
STRATEGY_CONFIGS = ["cumulative-drift"]
BASELINES = ["no_trade", "favorite", "threshold_move", "seeded_random"]
WINDOW_ID = "run001-pre_match-con040"
CLOSE_SEMANTICS = "pre_match"
COMMITTED_AT = "2026-07-04T12:15:51Z"
# The committed eligible-universe stamp (exact keys, not just counts). The run must reproduce it.
MANIFEST_CONTENT_HASH = "370287e4922428aae22692675d8eddd64c07519c7e223e82d5bf598c0cca02ea"

PACKS_DIR = Path(__file__).parent / "packs"
OUT_DIR = ROOT.parent / ".omc" / "research" / "edge-validation-runs"


def _manifest_content_hash(sink: dict) -> str:
    """Recompute the eligible-universe content hash EXACTLY as the committed artifact was built."""
    out = {"protocol_id": PROTOCOL_ID, "filter_config_hash": None, "fixtures": {}}
    for fid in FIXTURE_IDS:
        man = sink[fid]
        out["filter_config_hash"] = man.filter_config_hash
        out["fixtures"][str(fid)] = man.model_dump()
    return hashlib.sha256(json.dumps(out, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def run() -> dict:
    from veridex.backtest.evaluation import (  # noqa: PLC0415
        EvalProtocol,
        produce_results_by_fixture,
        run_multi_fixture_evaluation,
    )
    from veridex.strategies.market_quality import DEFAULT_MARKET_QUALITY_CONFIG  # noqa: PLC0415

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
            raise SystemExit(f"missing pack for fixture {fid} at {path}")

    manifest_sink: dict = {}
    results_by_fixture = await produce_results_by_fixture(
        protocol,
        packs=packs,
        market_quality_config=DEFAULT_MARKET_QUALITY_CONFIG,
        manifest_sink=manifest_sink,
    )

    # SELF-VERIFY: the scored universe must be the one we committed. Else the run is void.
    got = _manifest_content_hash(manifest_sink)
    if got != MANIFEST_CONTENT_HASH:
        raise SystemExit(
            f"VOID: eligible-universe hash mismatch — committed {MANIFEST_CONTENT_HASH}, got {got}. "
            "The scored universe diverged from the pre-run stamp; do NOT report this result."
        )

    report = run_multi_fixture_evaluation(protocol, results_by_fixture=results_by_fixture, cadence_ok=False)
    return {
        "protocol_id": PROTOCOL_ID,
        "committed_at": COMMITTED_AT,
        "manifest_content_hash": got,
        "universe_verified": True,
        "report": report,
    }


def main() -> None:
    out = asyncio.run(run())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "run-001-result.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out["report"], indent=2, default=str))
    print(f"\nuniverse_verified={out['universe_verified']} | manifest_content_hash={out['manifest_content_hash'][:12]}")


if __name__ == "__main__":
    main()
