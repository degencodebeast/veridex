"""C-6 — Run-002-VvV runner: framing honesty + self-verify gates (offline, synthetic).

These tests exercise the PURE, importable pieces of the predeclared Run-002-VvV runner — the
framing labels (CON-012 / step 6a), the self-verify coverage-hash gate (step 6, void-on-mismatch),
and the assembled run-result shape (decision_quote_coverage first-class, ``real_executable_edge_bps``
None, no CON-009 executable/fill wording). They NEVER load the 35MB operator frames and NEVER run the
real Run-002 (that is the operator step gated by a later Codex milestone review).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.txline_live.run002_vvv import (
    HEADLINE_FRESHNESS_S,
    HEADLINE_RUNWAY_LABEL,
    LOW_COVERAGE_CONCLUSION,
    NEAR_KICKOFF_LABEL,
    RunVoidError,
    build_run_result,
    headline_conclusion,
    near_kickoff_supported,
    runway_framing_label,
    stale_runway_diagnostic,
    verify_coverage_artifact_hash,
)
from veridex.backtest.venue_behavior_report import (
    DecisionQuoteCoverage,
    build_venue_behavior_report,
)

# --- CON-009 forbidden wording (measurement/execution claims a rung-2 report may NEVER make) ------
_FORBIDDEN_WORDS = ("executable", "fillability", "spread", "profit", "realized", "fill")


def _coverage(
    *,
    decision_count: int,
    quote_matched_count: int,
    used_buckets: dict[str, int],
    fired_pick_count: int = 0,
    fired_pick_quote_matched_count: int = 0,
) -> DecisionQuoteCoverage:
    quote_none = decision_count - quote_matched_count
    pct = round(quote_matched_count / decision_count * 100.0, 4) if decision_count else 0.0
    return DecisionQuoteCoverage(
        decision_count=decision_count,
        quote_matched_count=quote_matched_count,
        quote_none_count=quote_none,
        quote_matched_pct=pct,
        fired_pick_count=fired_pick_count,
        fired_pick_quote_matched_count=fired_pick_quote_matched_count,
        fired_pick_quote_none_count=fired_pick_count - fired_pick_quote_matched_count,
        freshness_bucket_counts_for_used_quotes=used_buckets,
    )


# ==========================================================================================
# 6a — the "near-kickoff" phrase is FORBIDDEN in the headline unless used quotes cluster near kickoff.
# ==========================================================================================


def test_headline_label_forbids_near_kickoff_when_used_quotes_are_runway_stale() -> None:
    # Used quotes cluster in the STALEST bucket (<=15m) — pre-match runway depth, NOT near kickoff.
    cov = _coverage(
        decision_count=100,
        quote_matched_count=10,
        used_buckets={"<=2m": 0, "<=5m": 0, "<=15m": 10},
    )
    assert near_kickoff_supported(cov) is False
    label = runway_framing_label(cov)
    assert label == HEADLINE_RUNWAY_LABEL
    assert "near-kickoff" not in label


def test_headline_label_allows_near_kickoff_only_when_buckets_prove_clustering() -> None:
    # Used quotes cluster in the freshest buckets (<=2m/<=5m) — clustering near kickoff is PROVEN.
    cov = _coverage(
        decision_count=100,
        quote_matched_count=10,
        used_buckets={"<=2m": 8, "<=5m": 1, "<=15m": 1},
    )
    assert near_kickoff_supported(cov) is True
    assert runway_framing_label(cov) == NEAR_KICKOFF_LABEL
    assert "near-kickoff" in NEAR_KICKOFF_LABEL


# ==========================================================================================
# 6a — a low quote_matched_pct is a COVERAGE statement, never a "no edge" MEASUREMENT statement.
# ==========================================================================================


def test_low_coverage_headline_is_a_coverage_statement_not_a_measurement() -> None:
    cov = _coverage(decision_count=100, quote_matched_count=12, used_buckets={"<=15m": 12})
    conclusion = headline_conclusion(cov)
    assert conclusion == LOW_COVERAGE_CONCLUSION
    # It must NOT claim "no edge" / "no dislocation" (a measurement the sparse run cannot make).
    assert "no edge" not in conclusion.lower()
    assert "no dislocation" not in conclusion.lower()


def test_adequate_coverage_headline_is_a_measurement_statement() -> None:
    cov = _coverage(decision_count=100, quote_matched_count=80, used_buckets={"<=15m": 80})
    conclusion = headline_conclusion(cov)
    assert conclusion != LOW_COVERAGE_CONCLUSION
    # Adequate coverage still may NOT assert "no edge" — it reports the distribution honestly.
    assert "no edge" not in conclusion.lower()


# ==========================================================================================
# 6a — a wider freshness bound is DIAGNOSTIC-ONLY: tagged, non-headline, freshness_s != 900.
# ==========================================================================================


def test_wider_freshness_bound_is_tagged_diagnostic_never_headline() -> None:
    diag = stale_runway_diagnostic(freshness_s=1800)
    assert diag["lane"] == "diagnostic"
    assert diag["headline"] is False
    assert diag["freshness_s"] == 1800
    assert diag["freshness_s"] != HEADLINE_FRESHNESS_S
    # The diagnostic lane must NOT inherit the headline runway label.
    assert diag.get("label") != HEADLINE_RUNWAY_LABEL


def test_headline_freshness_is_pinned_at_900() -> None:
    assert HEADLINE_FRESHNESS_S == 900


# ==========================================================================================
# Step 6 — the runner SELF-VERIFIES the coverage-artifact hash and VOIDS on any mismatch.
# ==========================================================================================


def _write_coverage_artifact(path: Path, *, tamper_hash: bool = False) -> str:
    """Write a minimal, correctly-hashed coverage artifact (probe canonicalization) and return its hash."""
    artifact: dict = {
        "tool": "cp1_probe/1",
        "min_pre_kickoff": 5,
        "fixtures": [],
        "headline_eligible_fixture_ids": [101, 102],
        "headline_eligible_count": 2,
        "viable": True,
    }
    canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
    true_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    artifact["artifact_content_hash"] = ("0" * 64) if tamper_hash else true_hash
    path.write_text(json.dumps(artifact, indent=1))
    return true_hash


def test_self_verify_returns_hash_when_coverage_matches(tmp_path: Path) -> None:
    cov_path = tmp_path / "cp1-coverage.json"
    true_hash = _write_coverage_artifact(cov_path)
    assert verify_coverage_artifact_hash(cov_path, committed=true_hash) == true_hash


def test_self_verify_voids_when_committed_hash_diverges(tmp_path: Path) -> None:
    cov_path = tmp_path / "cp1-coverage.json"
    _write_coverage_artifact(cov_path)
    with pytest.raises(RunVoidError):
        verify_coverage_artifact_hash(cov_path, committed="deadbeef" * 8)


def test_self_verify_voids_when_embedded_hash_is_tampered(tmp_path: Path) -> None:
    cov_path = tmp_path / "cp1-coverage.json"
    true_hash = _write_coverage_artifact(cov_path, tamper_hash=True)
    with pytest.raises(RunVoidError):
        verify_coverage_artifact_hash(cov_path, committed=true_hash)


# ==========================================================================================
# Step 6 / CON-003 / CON-009 — the assembled run result: decision_quote_coverage first-class,
# real_executable_edge_bps None, and no execution/fill wording in any human-readable string value.
# ==========================================================================================


def _report_and_coverage():
    decisions = _decisions()
    report = build_venue_behavior_report(
        [],
        decisions,
        haircut_ladder_bps=[0, 100, 200, 300],
        prob_bands=[(0, 100)],
        ttc_buckets=["<1h"],
        freshness_buckets=["<=2m", "<=5m", "<=15m"],
    )
    return report, report.decision_quote_coverage


def _decisions():
    from veridex.backtest.venue_behavior_report import VenueDecision

    return [
        VenueDecision(fired=True, quote_matched=True, staleness_s=120),
        VenueDecision(fired=True, quote_matched=False),
        VenueDecision(fired=False, quote_matched=False),
        VenueDecision(fired=False, quote_matched=True, staleness_s=600),
    ]


def test_run_result_has_first_class_decision_quote_coverage_and_null_real_edge() -> None:
    report, coverage = _report_and_coverage()
    result = build_run_result(
        protocol_id="run-002-vvv",
        committed_at="2026-07-04T00:00:00Z",
        venue_source_id="vsid#1",
        coverage_artifact_hash="cov#1",
        freshness_s=HEADLINE_FRESHNESS_S,
        behavior_report=report,
        decision_quote_coverage=coverage,
        haircut_ladder_bps=[0, 100, 200, 300],
    )
    # decision_quote_coverage is a FIRST-CLASS top-level field, not buried in a slice.
    assert "decision_quote_coverage" in result
    assert result["decision_quote_coverage"]["decision_count"] == coverage.decision_count
    # CON-003: no live fill in C/P1.
    assert result["real_executable_edge_bps"] is None
    # 6a: the run_note carries the fixed runway framing label.
    assert result["run_note"] == HEADLINE_RUNWAY_LABEL


def test_run_result_strings_never_use_execution_or_fill_wording() -> None:
    report, coverage = _report_and_coverage()
    result = build_run_result(
        protocol_id="run-002-vvv",
        committed_at="2026-07-04T00:00:00Z",
        venue_source_id="vsid#1",
        coverage_artifact_hash="cov#1",
        freshness_s=HEADLINE_FRESHNESS_S,
        behavior_report=report,
        decision_quote_coverage=coverage,
        haircut_ladder_bps=[0, 100, 200, 300],
    )

    def _string_values(obj):
        if isinstance(obj, str):
            yield obj
        elif isinstance(obj, dict):
            for v in obj.values():
                yield from _string_values(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                yield from _string_values(v)

    for text in _string_values(result):
        low = text.lower()
        for word in _FORBIDDEN_WORDS:
            assert word not in low, f"CON-009: forbidden word {word!r} in run-result string {text!r}"
