"""WD-5b — the enriched CheckResult taxonomy (spec §4.3 / SEC-001/002)."""

from __future__ import annotations

from veridex.checks.result import (
    CHECK_LABELS,
    CHECK_SEVERITY,
    CheckId,
    CheckResult,
)


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
