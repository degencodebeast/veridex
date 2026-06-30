"""WD-3 — the decoupled standalone-run core (one agent, no competition container)."""

from __future__ import annotations

from pathlib import Path

from veridex.chain.anchor import run_manifest_hash
from veridex.checks.build import build_check_results, check_results_to_proof_block
from veridex.ingest.marketstate import replay_marketstates
from veridex.runtime.competition import SCHEMA_VERSIONS
from veridex.runtime.orchestrator import run_competition
from veridex.scoring import score_run
from veridex.strategies.momentum import momentum_agent
from veridex.verifier.recompute import (
    fixture_or_window_id_from_events,
    manifest_from_run,
    recompute_score_root,
)
from veridex_agent.run import StandaloneRunResult, standalone_run

FIXTURE = str(Path(__file__).parent / "fixtures" / "wd2_momentum_replay.json")


async def test_standalone_run_produces_a_verified_proof() -> None:
    ticks = replay_marketstates(FIXTURE)
    result = await standalone_run(ticks, momentum_agent("mom"), source_mode="replay", anchor_fn=None)
    assert isinstance(result, StandaloneRunResult)
    assert result.source_mode == "replay"
    assert result.anchor_status == "not_anchored"  # anchor_fn=None → offline
    assert result.verified is True
    assert result.verify_report["evidence_match"] is True
    assert "checks" in result.proof_card
    assert len(result.scores) == 1  # exactly one agent — no competition/ranking framing
    # FULL arena parity: the manifest is passed so MANIFEST_BOUND gets a real verdict (not n/a);
    # offline replay → ANCHOR is honestly not_applicable.
    checks = result.proof_card["checks"]
    assert checks["manifest_bound"]["result"] == "pass"
    assert checks["anchor"]["result"] == "not_applicable"


async def test_standalone_run_anchors_when_anchor_fn_supplied() -> None:
    ticks = replay_marketstates(FIXTURE)

    async def fake_anchor(manifest_hash: str) -> str:
        assert len(manifest_hash) == 64
        return "FAKESIG"

    result = await standalone_run(ticks, momentum_agent("mom"), source_mode="replay", anchor_fn=fake_anchor)
    assert result.anchor_status == "anchored"
    assert result.signature == "FAKESIG"
    assert result.proof_card["anchor"]["signature"] == "FAKESIG"
    # Anchored → ANCHOR and MANIFEST_BOUND both pass (real arena-parity verdicts).
    checks = result.proof_card["checks"]
    assert checks["anchor"]["result"] == "pass"
    assert checks["manifest_bound"]["result"] == "pass"


async def test_standalone_manifest_bound_is_falsifiable() -> None:
    # Proves MANIFEST_BOUND is HONEST (not a tautological pass): it independently recomputes the
    # score-root + manifest hash from this run+scores, so a TAMPERED manifest FAILS the check. This
    # is the scoring-time binding (the arena pattern), distinct from the WD-1 verify false-pass case.
    ticks = replay_marketstates(FIXTURE)
    run = await run_competition(ticks, [momentum_agent("mom")], source_mode="replay")
    scores = score_run(run)
    manifest = manifest_from_run(
        run,
        fixture_or_window_id=fixture_or_window_id_from_events(run.run_events),
        score_root=recompute_score_root(scores),
        schema_versions=dict(SCHEMA_VERSIONS),
    )
    manifest_hash = run_manifest_hash(manifest)
    anchor = {"status": "anchored", "signature": "S", "cluster": "devnet"}

    ok = check_results_to_proof_block(
        build_check_results(
            scores=scores, run=run, manifest=manifest, manifest_hash=manifest_hash, anchor=anchor, source_mode="replay"
        )
    )
    assert ok["manifest_bound"]["result"] == "pass"
    assert ok["anchor"]["result"] == "pass"

    # Tamper the manifest's evidence root → the independent recompute diverges → MANIFEST_BOUND FAILS.
    tampered = {**manifest, "action_evidence_root": "0" * 64}
    bad = check_results_to_proof_block(
        build_check_results(
            scores=scores, run=run, manifest=tampered, manifest_hash=manifest_hash, anchor=anchor, source_mode="replay"
        )
    )
    assert bad["manifest_bound"]["result"] == "fail"
