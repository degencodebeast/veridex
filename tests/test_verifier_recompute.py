"""WD-1 — authoritative verify/recompute over a sealed RunResult."""

from __future__ import annotations

import json

from veridex.api.demo_fixtures import build_demo_ticks, contrarian_agent
from veridex.runtime.competition import run_demo_competition
from veridex.runtime.orchestrator import deterministic_agent
from veridex.verifier.recompute import (
    VerifyReport,
    fixture_or_window_id_from_events,
    manifest_from_run,
    recompute_score_root,
    verify_run,
)


async def _sealed_run():
    ticks = build_demo_ticks()
    agents = [deterministic_agent("agent-alpha"), contrarian_agent("agent-beta")]
    return await run_demo_competition(ticks, agents, source_mode="replay", anchor_fn=None)


async def test_verify_run_confirms_a_clean_run() -> None:
    result = await _sealed_run()
    report = verify_run(result.run)
    assert isinstance(report, VerifyReport)
    assert report.evidence_match is True
    assert report.evidence_hash_recomputed == result.run.evidence_hash
    assert report.verified is True
    assert report.score_root == recompute_score_root(result.scores)


async def test_verify_run_manifest_matches_anchored_manifest() -> None:
    result = await _sealed_run()
    report = verify_run(result.run)
    # The verifier's reconstructed manifest hash must equal the harness-anchored one.
    assert report.manifest_hash == result.manifest_hash


async def test_verify_run_detects_tampered_evidence() -> None:
    result = await _sealed_run()
    tampered = result.run
    # Mutate a sealed event payload after sealing → recomputed hash diverges.
    tampered.run_events[0]["state_snapshot_json"] = '{"tampered":true}'
    report = verify_run(tampered)
    assert report.evidence_match is False
    assert report.verified is False


async def test_fixture_or_window_id_from_events_reads_first_tick() -> None:
    result = await _sealed_run()
    fid = fixture_or_window_id_from_events(result.run.run_events)
    assert fid != "unknown"


async def test_verify_run_is_read_only_over_the_seal() -> None:
    # The sealed evidence prefix must be byte-identical before/after verify (verify never writes).
    result = await _sealed_run()
    before_hash = result.run.evidence_hash
    before_events = json.dumps(result.run.run_events, sort_keys=True)
    verify_run(result.run)
    assert result.run.evidence_hash == before_hash
    assert json.dumps(result.run.run_events, sort_keys=True) == before_events


def test_manifest_from_run_is_pure_dict() -> None:
    # manifest_from_run never re-scores; it accepts the precomputed score_root.
    class _Stub:
        run_id = "r1"
        agent_ids = ["a"]
        evidence_hash = "e" * 64
        proof_mode_map = {"a": "reproducible"}

    manifest = manifest_from_run(_Stub(), fixture_or_window_id="123", score_root="s" * 64)  # type: ignore[arg-type]
    assert manifest["run_id"] == "r1"
    assert manifest["action_evidence_root"] == "e" * 64
    assert manifest["score_root"] == "s" * 64
