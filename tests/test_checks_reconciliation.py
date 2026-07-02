"""REQ-2D-801 / AC-2D-801 — single typed CheckResult builder invariant (checks reconciliation).

Pins two invariants:

  1. No path constructs a legacy/divergent dict of checks — there is exactly ONE builder
     (:func:`~veridex.checks.build.build_check_results`). The former ``_default_checks``
     name (a thin 2-arg convenience wrapper over that builder, used by manifest-unavailable
     API read endpoints) is retired in favor of a name that says what it actually is.
  2. The read-path and full-path checks blocks derive from the SAME 7-CheckId taxonomy and
     differ ONLY by input-availability (manifest/anchor/events absent vs. present) — never
     by divergent verdict logic.
"""

from __future__ import annotations

import hashlib

from tests._arena_fixtures import finished_run_result
from veridex.chain.anchor import run_manifest, run_manifest_hash
from veridex.checks.build import build_check_results, check_results_to_proof_block
from veridex.checks.result import CheckId
from veridex.competition.events import build_policy_result_event
from veridex.policy.engine import PolicyDecision
from veridex.runtime import competition
from veridex.runtime.evidence import serialize_payload
from veridex.scoring import score_run


def test_default_checks_name_retired() -> None:
    """AC-2D-801: the misleading ``_default_checks`` name no longer exists on the module.

    Pins the rename — nothing should ever again import/construct checks through a name
    that implies a "default"/legacy dict-based path distinct from the typed builder.
    """
    assert not hasattr(competition, "_default_checks")


def _manifest_for(run: object, scores: list[dict[str, object]]) -> dict[str, object]:
    return run_manifest(
        run_id=run.run_id,  # type: ignore[attr-defined]
        fixture_or_window_id="fx",
        agent_ids=run.agent_ids,  # type: ignore[attr-defined]
        action_evidence_root=run.evidence_hash,  # type: ignore[attr-defined]
        score_root=hashlib.sha256(serialize_payload(scores).encode()).hexdigest(),
        proof_mode_map=run.proof_mode_map,  # type: ignore[attr-defined]
        code_prompt_schema_versions={"verifier": "v0"},
    )


def _full_path_events() -> list[dict[str, object]]:
    """One real, approved policy_result event (via the production constructor).

    Enough to move POLICY_OBEYED and RECEIPT_SEPARATION off ``not_applicable`` without
    triggering a bypass fail — isolates the input-availability difference under test.
    """
    approved = build_policy_result_event(
        competition_id="c",
        run_id="run",
        seq=1,
        event_ts=0,
        agent_id="a",
        source_sequence_no_ref=1,
        policy_result_payload={
            "decision": PolicyDecision.APPROVED.value,
            "reason_codes": [],
            "policy_hash": "ph",
        },
        execution_id="run:1",
    )
    return [approved.model_dump(mode="json")]


def _full_path_block(run: object, scores: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    manifest = _manifest_for(run, scores)
    anchor = {"status": "anchored", "signature": "sig123", "cluster": "devnet"}
    return check_results_to_proof_block(
        build_check_results(
            scores=scores,
            run=run,  # type: ignore[arg-type]
            manifest=manifest,
            manifest_hash=run_manifest_hash(manifest),
            anchor=anchor,
            events=_full_path_events(),
            source_mode=run.source_mode,  # type: ignore[attr-defined]
        )
    )


def test_read_path_and_full_path_share_the_same_seven_check_ids() -> None:
    """Both blocks derive from the ONE builder and yield the identical 7-CheckId taxonomy."""
    run = finished_run_result()
    scores = score_run(run)

    read_path_block = competition.read_path_check_block(scores, run)
    full_path_block = _full_path_block(run, scores)

    frozen_ids = {c.value for c in CheckId}
    assert set(read_path_block) == frozen_ids
    assert set(full_path_block) == frozen_ids


def test_read_path_vs_full_path_differ_only_by_input_availability() -> None:
    """MANIFEST_BOUND/POLICY_OBEYED/RECEIPT_SEPARATION are not_applicable ONLY when their
    inputs (manifest/anchor/events) are absent — the read path never diverges in logic,
    only in what it was handed.
    """
    run = finished_run_result()
    scores = score_run(run)

    read_path_block = competition.read_path_check_block(scores, run)
    assert read_path_block[CheckId.MANIFEST_BOUND.value]["result"] == "not_applicable"
    assert read_path_block[CheckId.POLICY_OBEYED.value]["result"] == "not_applicable"
    assert read_path_block[CheckId.RECEIPT_SEPARATION.value]["result"] == "not_applicable"

    full_path_block = _full_path_block(run, scores)
    assert full_path_block[CheckId.MANIFEST_BOUND.value]["result"] != "not_applicable"
    assert full_path_block[CheckId.POLICY_OBEYED.value]["result"] != "not_applicable"
    assert full_path_block[CheckId.RECEIPT_SEPARATION.value]["result"] != "not_applicable"

    # Every check outside those three is unaffected by input-availability (both agree).
    unaffected = frozenset(CheckId) - {
        CheckId.MANIFEST_BOUND,
        CheckId.POLICY_OBEYED,
        CheckId.RECEIPT_SEPARATION,
        CheckId.ANCHOR,  # read path passes no anchor -> not_applicable; full path passes one -> pass
    }
    for check_id in unaffected:
        assert read_path_block[check_id.value]["result"] == full_path_block[check_id.value]["result"]
