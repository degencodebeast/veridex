"""WD-5b — the enriched CheckResult taxonomy (spec §4.3 / SEC-001/002)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from tests._arena_fixtures import finished_run_result
from veridex.chain.anchor import run_manifest, run_manifest_hash
from veridex.checks.build import (
    build_check_results,
    build_performance_metrics,
    check_results_to_proof_block,
)
from veridex.checks.result import (
    CHECK_LABELS,
    CHECK_SEVERITY,
    CheckId,
    CheckResult,
)
from veridex.competition.events import (
    build_execution_receipt_event,
    build_execution_submitted_event,
    build_policy_result_event,
)
from veridex.policy.engine import PolicyDecision
from veridex.runtime.evidence import compute_evidence_hash, serialize_payload
from veridex.scoring import score_run


def test_check_id_is_frozen_seven() -> None:
    assert [c.value for c in CheckId] == [
        "evidence_integrity",
        "llm_boundary",
        "metrics_recomputed",
        "manifest_bound",
        "policy_obeyed",
        "receipt_separation",
        "anchor",
    ]


def test_metrics_recomputed_ui_label() -> None:
    assert CHECK_LABELS[CheckId.METRICS_RECOMPUTED] == "Score Recomputed"


def test_anchor_is_info_severity() -> None:
    assert CHECK_SEVERITY[CheckId.ANCHOR] == "info"
    assert CHECK_SEVERITY[CheckId.EVIDENCE_INTEGRITY] == "blocking"


def test_check_result_round_trips() -> None:
    cr = CheckResult(
        id=CheckId.EVIDENCE_INTEGRITY,
        label="Evidence Integrity",
        result="pass",
        severity="blocking",
        method="sha256_evidence_hash",
        scope="run_events",
    )
    dumped = cr.model_dump(mode="json")
    assert dumped["id"] == "evidence_integrity"
    assert dumped["result"] == "pass"
    assert dumped["evidence_refs"] == [] and dumped["rules"] == []
    assert dumped["details"] == {} and dumped["error"] is None


def test_check_labels_cover_every_check_id() -> None:
    # Map-completeness guard: a new CheckId without a label fails the suite.
    assert set(CHECK_LABELS) == set(CheckId)


def test_check_severity_covers_every_check_id() -> None:
    # Map-completeness guard: a new CheckId without a severity fails the suite.
    assert set(CHECK_SEVERITY) == set(CheckId)


def test_builder_returns_all_seven_in_order() -> None:
    run = finished_run_result()
    results = build_check_results(scores=score_run(run), run=run, source_mode="replay")
    assert [r.id for r in results] == list(CheckId)


def test_evidence_integrity_pass_on_clean_run() -> None:
    run = finished_run_result()
    results = {r.id: r for r in build_check_results(scores=score_run(run), run=run)}
    ei = results[CheckId.EVIDENCE_INTEGRITY]
    assert ei.result == "pass" and ei.details["recomputed_match"] is True


def test_evidence_integrity_fail_on_tamper() -> None:
    run = finished_run_result()
    scores = score_run(run)
    run.run_events[0]["_tampered"] = "x"
    ei = {r.id: r for r in build_check_results(scores=scores, run=run)}[CheckId.EVIDENCE_INTEGRITY]
    assert ei.result == "fail" and ei.details["recomputed_match"] is False


def test_evidence_integrity_fails_closed_on_dup_seq() -> None:
    run = finished_run_result()
    scores = score_run(run)
    run.run_events.append(dict(run.run_events[0]))  # duplicate sequence_no
    ei = {r.id: r for r in build_check_results(scores=scores, run=run)}[CheckId.EVIDENCE_INTEGRITY]
    assert ei.result == "fail" and ei.error is not None


def test_llm_boundary_pass_on_clean_trust_path() -> None:
    run = finished_run_result()
    lb = {r.id: r for r in build_check_results(scores=score_run(run), run=run)}[CheckId.LLM_BOUNDARY]
    assert lb.result == "pass" and lb.method == "static_import_audit"


def test_llm_boundary_fails_closed_on_missing_trust_target(monkeypatch: pytest.MonkeyPatch) -> None:
    # CON-2B-02 fail-closed parity: a deleted/renamed trust dir must FAIL, not vacuously PASS.
    # `Path.rglob` over a nonexistent directory yields nothing, so without an existence guard
    # the audit silently passes (fail-OPEN, inverted from SEC-002). Point the audit at a missing
    # target and assert LLM_BOUNDARY reports `fail` with a populated error.
    import veridex.checks.build as build_mod

    missing = build_mod._VERIDEX_PKG / "nonexistent_trust_dir"
    assert not missing.exists()
    monkeypatch.setattr(build_mod, "_TRUST_TARGETS", (*build_mod._TRUST_TARGETS, missing))
    run = finished_run_result()
    lb = {r.id: r for r in build_check_results(scores=score_run(run), run=run)}[CheckId.LLM_BOUNDARY]
    assert lb.result == "fail" and lb.error is not None


def test_metrics_recomputed_pass_when_table_matches() -> None:
    run = finished_run_result()
    scores = score_run(run)
    mr = {r.id: r for r in build_check_results(scores=scores, run=run)}[CheckId.METRICS_RECOMPUTED]
    assert mr.result == "pass"


def test_metrics_recomputed_fail_on_tampered_score_row() -> None:
    run = finished_run_result()
    scores = score_run(run)
    scores[0]["avg_clv_bps"] = (scores[0]["avg_clv_bps"] or 0) + 9999  # tamper visible table
    mr = {r.id: r for r in build_check_results(scores=scores, run=run)}[CheckId.METRICS_RECOMPUTED]
    assert mr.result == "fail"


def test_proof_block_is_keyed_by_check_id() -> None:
    run = finished_run_result()
    block = check_results_to_proof_block(build_check_results(scores=score_run(run), run=run))
    assert set(block) == {c.value for c in CheckId}
    assert "clv" not in block  # SEC-001: CLV is never a check


def test_performance_metrics_carry_clv_not_checks() -> None:
    run = finished_run_result()
    metrics = build_performance_metrics(score_run(run))
    assert "clv" in metrics and "max_drawdown" in metrics and "hit_rate" in metrics


def _manifest_for(run, scores):
    return run_manifest(
        run_id=run.run_id,
        fixture_or_window_id="fx",
        agent_ids=run.agent_ids,
        action_evidence_root=run.evidence_hash,
        score_root=hashlib.sha256(serialize_payload(scores).encode()).hexdigest(),
        proof_mode_map=run.proof_mode_map,
        code_prompt_schema_versions={"verifier": "v0"},
    )


def test_manifest_bound_not_applicable_without_manifest() -> None:
    run = finished_run_result()
    mb = {r.id: r for r in build_check_results(scores=score_run(run), run=run)}[CheckId.MANIFEST_BOUND]
    assert mb.result == "not_applicable"


def test_manifest_bound_pass_on_consistent_manifest() -> None:
    run = finished_run_result()
    scores = score_run(run)
    manifest = _manifest_for(run, scores)
    mb = {
        r.id: r
        for r in build_check_results(
            scores=scores, run=run, manifest=manifest, manifest_hash=run_manifest_hash(manifest)
        )
    }[CheckId.MANIFEST_BOUND]
    assert mb.result == "pass"


def test_manifest_bound_fail_on_wrong_evidence_root() -> None:
    run = finished_run_result()
    scores = score_run(run)
    manifest = _manifest_for(run, scores)
    manifest["action_evidence_root"] = "deadbeef"
    mb = {r.id: r for r in build_check_results(scores=scores, run=run, manifest=manifest)}[CheckId.MANIFEST_BOUND]
    assert mb.result == "fail" and any("action_evidence_root" in str(rule) for rule in mb.rules)


def test_manifest_bound_fails_closed_on_unserializable_manifest() -> None:
    # CON-2B-02: a manifest that cannot be canonically serialized must yield a `fail`
    # CheckResult with a populated error, never propagate an exception out of the pass.
    run = finished_run_result()
    scores = score_run(run)
    manifest = {"run_id": run.run_id, "nonserializable": {1, 2, 3}}  # set is not JSON-serializable
    mb = {r.id: r for r in build_check_results(scores=scores, run=run, manifest=manifest, manifest_hash="deadbeef")}[
        CheckId.MANIFEST_BOUND
    ]
    assert mb.result == "fail" and mb.error is not None


def test_anchor_not_applicable_offline_replay() -> None:
    run = finished_run_result()
    a = {r.id: r for r in build_check_results(scores=score_run(run), run=run, source_mode="replay")}[CheckId.ANCHOR]
    assert a.result == "not_applicable"


def test_anchor_pending_when_live_unanchored() -> None:
    run = finished_run_result()
    a = {r.id: r for r in build_check_results(scores=score_run(run), run=run, source_mode="live")}[CheckId.ANCHOR]
    assert a.result == "pending"


def test_anchor_pass_when_anchored() -> None:
    run = finished_run_result()
    anchor = {"status": "anchored", "signature": "sig123", "cluster": "devnet"}
    a = {r.id: r for r in build_check_results(scores=score_run(run), run=run, anchor=anchor)}[CheckId.ANCHOR]
    assert a.result == "pass"


# ---------------------------------------------------------------------------
# Task 4 — POLICY_OBEYED + RECEIPT_SEPARATION (from REAL derived-event producers).
#
# These build the derived events via the SAME constructors the executor lane uses
# (`build_policy_result_event` / `build_execution_submitted_event` /
# `build_execution_receipt_event`) — NEVER hand-injecting fields — so the checks are
# proven non-inert against the production event shape, not a synthetic fixture.
# ---------------------------------------------------------------------------


def _real_exec_events(*, bypass: bool = False) -> list[dict[str, Any]]:
    """Two executions via the real producers: one DENIED (``run:3``), one APPROVED+filled (``run:5``).

    When ``bypass`` is True the DENIED execution ``run:3`` ALSO gets an ``execution_submitted``
    — the exact denied-then-submitted bypass POLICY_OBEYED must catch.
    """
    denied = build_policy_result_event(
        competition_id="c",
        run_id="run",
        seq=1,
        event_ts=0,
        agent_id="a",
        source_sequence_no_ref=3,
        policy_result_payload={
            "decision": PolicyDecision.DENIED.value,
            "reason_codes": ["slippage_over_max"],
            "policy_hash": "ph",
        },
        execution_id="run:3",
    )
    approved = build_policy_result_event(
        competition_id="c",
        run_id="run",
        seq=2,
        event_ts=0,
        agent_id="b",
        source_sequence_no_ref=5,
        policy_result_payload={
            "decision": PolicyDecision.APPROVED.value,
            "reason_codes": [],
            "policy_hash": "ph",
        },
        execution_id="run:5",
    )
    submitted = build_execution_submitted_event(
        competition_id="c",
        run_id="run",
        seq=3,
        event_ts=0,
        execution_id="run:5",
        payload={"execution_id": "run:5", "agent_id": "b"},
    )
    receipt = build_execution_receipt_event(
        competition_id="c",
        run_id="run",
        seq=4,
        event_ts=0,
        execution_id="run:5",
        receipt_payload={"execution_id": "run:5", "status": "filled"},
    )
    events = [denied, approved, submitted, receipt]
    if bypass:
        events.append(
            build_execution_submitted_event(
                competition_id="c",
                run_id="run",
                seq=5,
                event_ts=0,
                execution_id="run:3",
                payload={"execution_id": "run:3", "agent_id": "a"},
            )
        )
    return [e.model_dump(mode="json") for e in events]


def test_policy_obeyed_pass_no_bypass() -> None:
    run = finished_run_result()
    po = {r.id: r for r in build_check_results(scores=score_run(run), run=run, events=_real_exec_events())}[
        CheckId.POLICY_OBEYED
    ]
    assert po.result == "pass"
    assert po.details["denied_count"] == 1


def test_policy_obeyed_fail_on_denied_then_submitted() -> None:
    # The whole point: a DENIED policy result whose execution was nonetheless submitted.
    run = finished_run_result()
    po = {r.id: r for r in build_check_results(scores=score_run(run), run=run, events=_real_exec_events(bypass=True))}[
        CheckId.POLICY_OBEYED
    ]
    assert po.result == "fail"
    assert any("run:3" in str(rule) for rule in po.rules)


def test_receipt_separation_pass_when_all_derived() -> None:
    run = finished_run_result()
    rs = {r.id: r for r in build_check_results(scores=score_run(run), run=run, events=_real_exec_events())}[
        CheckId.RECEIPT_SEPARATION
    ]
    assert rs.result == "pass"


def test_receipt_separation_fail_when_receipt_is_evidence() -> None:
    run = finished_run_result()
    events = _real_exec_events()
    for e in events:
        if e["event_type"] == "execution_receipt":
            e["evidence"] = True  # a receipt leaked into the evidence prefix (SEC-004 violation)
    rs = {r.id: r for r in build_check_results(scores=score_run(run), run=run, events=events)}[
        CheckId.RECEIPT_SEPARATION
    ]
    assert rs.result == "fail"
    assert any("execution_receipt" in str(rule) for rule in rs.rules)


def test_policy_and_receipt_not_applicable_without_events() -> None:
    run = finished_run_result()
    results = {r.id: r for r in build_check_results(scores=score_run(run), run=run)}
    assert results[CheckId.POLICY_OBEYED].result == "not_applicable"
    assert results[CheckId.RECEIPT_SEPARATION].result == "not_applicable"


def test_evidence_hash_independent_of_policy_result_enrichment() -> None:
    # AC-213 sealed-prefix parity: the policy_result enrichment lives in the derived,
    # evidence=False tail — recomputing the sealed evidence hash is byte-identical.
    run = finished_run_result()
    sealed = compute_evidence_hash(run.run_events)
    assert sealed == run.evidence_hash
    _ = _real_exec_events(bypass=True)  # building enriched derived events touches nothing sealed
    assert compute_evidence_hash(run.run_events) == sealed
