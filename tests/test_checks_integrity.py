"""WD-5a — checks integrity: evidence_integrity recomputes hash; llm_boundary runs real audit.

RED → GREEN tests written strictly before the implementation fix. Both checks in
``_default_checks`` were no-ops prior to this fix:

  * ``evidence_integrity`` only asserted non-empty hash — never recomputed, so a tampered
    run with a stale hash still showed ✅.
  * ``llm_boundary`` was hardcoded ``"pass"`` — the real import audit never ran.

TDD discipline: RED output is recorded in the commit message. Tests are written here and
watched fail (wrong verdict), then the minimal implementation turns them GREEN.
"""

from __future__ import annotations

import copy

import pytest

from tests._arena_fixtures import finished_run_result
from veridex.checks.build import build_performance_metrics
from veridex.runtime.competition import _default_checks
from veridex.runtime.evidence import compute_evidence_hash
from veridex.scoring import score_run

# ---------------------------------------------------------------------------
# evidence_integrity — fail-closed when run_events diverge from evidence_hash
# ---------------------------------------------------------------------------


def test_evidence_integrity_fails_on_tampered_run() -> None:
    """_default_checks must return ``"fail"`` when run_events no longer match evidence_hash.

    Tamper: inject a new key into ``run_events[0]`` so the recomputed SHA-256 diverges from
    the stored ``evidence_hash`` (which was sealed over the original events).

    RED: before WD-5a the check only tested ``bool(run.evidence_hash)`` so it returned
    ``"pass"`` on a tampered run. After WD-5a it recomputes and fails-closed.
    """
    run = finished_run_result()
    scores = score_run(run)

    # Tamper: mutate a dict inside the list — frozen dataclass can't stop this.
    # The stored evidence_hash is now stale (sealed before the mutation).
    run.run_events[0]["_tampered"] = "injected_by_test"

    checks = _default_checks(scores, run)
    assert checks["evidence_integrity"]["result"] == "fail"


def test_evidence_integrity_passes_on_clean_run() -> None:
    """_default_checks must return ``"pass"`` on an unmodified (clean) run.

    The recomputed hash must equal the stored evidence_hash for a run that was
    not tampered with after sealing.
    """
    run = finished_run_result()
    scores = score_run(run)

    checks = _default_checks(scores, run)
    assert checks["evidence_integrity"]["result"] == "pass"


def test_evidence_integrity_exposes_recomputed_match_flag() -> None:
    """The ``recomputed_match`` boolean surfaces the comparison result on the card."""
    run = finished_run_result()
    scores = score_run(run)

    # Clean run: recomputed_match must be True (now nested under details — typed CheckResult).
    checks = _default_checks(scores, run)
    assert checks["evidence_integrity"]["details"]["recomputed_match"] is True

    # Tampered run: recomputed_match must be False.
    run2 = finished_run_result()
    scores2 = score_run(run2)
    run2.run_events[0]["_tampered"] = "injected_by_test"
    checks2 = _default_checks(scores2, run2)
    assert checks2["evidence_integrity"]["details"]["recomputed_match"] is False


# ---------------------------------------------------------------------------
# llm_boundary — runs the real import audit; catches violations; fail-closed
# ---------------------------------------------------------------------------


def test_llm_boundary_fails_when_audit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """_default_checks must return ``"fail"`` when ``assert_no_llm_imports`` raises.

    Monkeypatches the function in the ``checks.build`` namespace (where ``_default_checks`` now
    delegates the audit) so the try/except inside ``_llm_boundary`` catches the injected
    ``AssertionError``.

    RED: before WD-5a the check was hardcoded ``"pass"``, ignoring any exception entirely.
    After WD-5a/WD-5b it runs the real audit (in ``checks/build.py``) and catches violations.
    """
    import veridex.checks.build as build_mod

    def _always_raise(path: object) -> None:
        raise AssertionError("Forbidden LLM import 'openai' in fake.py (injected by test)")

    monkeypatch.setattr(build_mod, "assert_no_llm_imports", _always_raise)

    run = finished_run_result()
    scores = score_run(run)

    checks = _default_checks(scores, run)
    assert checks["llm_boundary"]["result"] == "fail"


def test_llm_boundary_passes_on_clean_trust_path() -> None:
    """_default_checks must return ``"pass"`` when the real trust path is LLM-SDK-free.

    Exercises the real ``assert_no_llm_imports`` over the real trust-path targets — this
    is the live integration smoke-test that proves the boundary is actually enforced.
    """
    run = finished_run_result()
    scores = score_run(run)

    checks = _default_checks(scores, run)
    assert checks["llm_boundary"]["result"] == "pass"


# ---------------------------------------------------------------------------
# Fail-closed: malformed evidence must not crash card-build (WD-5a codex review)
# ---------------------------------------------------------------------------


def test_evidence_integrity_fails_closed_on_duplicate_sequence_no() -> None:
    """Duplicate sequence_no raises ValueError inside compute_evidence_hash.

    The card must absorb it and return result=``"fail"`` with recomputed_match=False
    and a populated ``error`` field — never propagate the exception.

    RED: before this fix compute_evidence_hash raises and card-build crashes.
    """
    run = finished_run_result()
    scores = score_run(run)

    # Inject a second copy of the first event — same sequence_no, duplicate.
    run.run_events.append(dict(run.run_events[0]))

    checks = _default_checks(scores, run)
    assert checks["evidence_integrity"]["result"] == "fail"
    assert checks["evidence_integrity"]["details"]["recomputed_match"] is False
    assert checks["evidence_integrity"]["error"] is not None


def test_evidence_integrity_fails_closed_on_missing_sequence_no() -> None:
    """Missing sequence_no raises KeyError inside compute_evidence_hash sort key.

    The card must absorb it and return result=``"fail"``.

    RED: before this fix the KeyError propagates and card-build crashes.
    """
    run = finished_run_result()
    scores = score_run(run)

    # Remove the sequence_no key from the first event so the sort-key lambda KeyErrors.
    del run.run_events[0]["sequence_no"]

    checks = _default_checks(scores, run)
    assert checks["evidence_integrity"]["result"] == "fail"
    assert checks["evidence_integrity"]["details"]["recomputed_match"] is False


def test_llm_boundary_fails_closed_on_non_assertion_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """assert_no_llm_imports may also raise SyntaxError/OSError on malformed source.

    The narrow ``except AssertionError`` lets those propagate; the card must
    treat any audit exception as a boundary failure, not a crash.

    RED: before this fix a SyntaxError escapes the except clause and crashes card-build.
    """
    import veridex.checks.build as build_mod

    def _raise_syntax_error(path: object) -> None:
        raise SyntaxError("Fake syntax error in trust-path file (injected by test)")

    monkeypatch.setattr(build_mod, "assert_no_llm_imports", _raise_syntax_error)

    run = finished_run_result()
    scores = score_run(run)

    checks = _default_checks(scores, run)
    assert checks["llm_boundary"]["result"] == "fail"


# ---------------------------------------------------------------------------
# WD-5b — SEC-001 shape (7 CheckIds, CLV NOT a check) + evidence boundary
# ---------------------------------------------------------------------------


def test_default_checks_emits_seven_check_ids_and_no_clv() -> None:
    """SEC-001: ``_default_checks`` emits exactly the 7 frozen CheckIds; CLV is NOT one of them.

    The 2-arg convenience path has no manifest/anchor/events, so the manifest/policy/receipt
    checks are ``not_applicable`` and ANCHOR follows the run's source_mode (replay→na).
    """
    run = finished_run_result()
    scores = score_run(run)

    checks = _default_checks(scores, run)
    assert set(checks) == {
        "evidence_integrity",
        "llm_boundary",
        "metrics_recomputed",
        "manifest_bound",
        "policy_obeyed",
        "receipt_separation",
        "anchor",
    }
    assert "clv" not in checks  # SEC-001: CLV is a performance metric, never a check
    # CLV lives in the separate Performance-Metrics block instead.
    assert "clv" in build_performance_metrics(scores)


def test_default_checks_does_not_mutate_sealed_evidence() -> None:
    """The migration changes the proof-card representation, NOT the sealed evidence.

    Building checks + metrics must leave ``run.run_events`` and ``run.evidence_hash``
    byte-identical, and the sealed prefix must still recompute to the same hash.
    """
    run = finished_run_result()
    scores = score_run(run)

    evidence_hash_before = run.evidence_hash
    events_before = copy.deepcopy(run.run_events)

    _default_checks(scores, run)
    build_performance_metrics(scores)

    assert run.evidence_hash == evidence_hash_before
    assert run.run_events == events_before
    # The sealed RunEvent prefix still recomputes to the identical evidence_hash.
    assert compute_evidence_hash(run.run_events) == evidence_hash_before


# ---------------------------------------------------------------------------
# METRICS_RECOMPUTED — falsifiable (SEC-002): a tampered persisted clv_bps fails
# ---------------------------------------------------------------------------


def _first_scored_index(run: object) -> int:
    """Index of the first genuinely-scored row (valid + numeric clv_bps, not a bool/sentinel)."""
    for i, r in enumerate(run.score_rows):  # type: ignore[attr-defined]
        clv = r.get("clv_bps")
        if r.get("valid") is True and isinstance(clv, int) and not isinstance(clv, bool):
            return i
    raise AssertionError("fixture has no scored row to tamper")


def test_metrics_recomputed_passes_on_untampered_run() -> None:
    """The honest case: the displayed score table re-derives faithfully from sealed evidence."""
    run = finished_run_result()
    scores = score_run(run)
    assert _default_checks(scores, run)["metrics_recomputed"]["result"] == "pass"


def test_metrics_recomputed_fails_on_tampered_persisted_clv() -> None:
    """SEC-002 falsifiability: tampering a persisted ``clv_bps`` in ``run.score_rows`` (the value
    that feeds the displayed leaderboard avg) must flip METRICS_RECOMPUTED to ``fail``.

    The fresh recompute re-derives clv from the SEALED RunEvents (market snapshots + the sealed
    decision action) via the deterministic law — NOT from the stored clv — so a doctored score row
    no longer matches. ``score_rows`` is NOT part of the hashed evidence prefix, so
    ``evidence_integrity`` still passes; only METRICS_RECOMPUTED catches this tamper class.
    """
    run = finished_run_result()
    idx = _first_scored_index(run)
    run.score_rows[idx]["clv_bps"] = run.score_rows[idx]["clv_bps"] + 9999  # doctor the displayed metric

    # The displayed aggregate now reflects the tamper (score_run reads score_rows).
    tampered_scores = score_run(run)
    checks = _default_checks(tampered_scores, run)

    assert checks["metrics_recomputed"]["result"] == "fail"
    assert checks["metrics_recomputed"]["error"] is not None
    # the per-row discrepancy is surfaced in rules (persisted vs recomputed).
    assert any("recomputed_clv_bps" in rule for rule in checks["metrics_recomputed"]["rules"])
    # the tamper did NOT touch the hashed evidence prefix, so evidence_integrity stays pass.
    assert checks["evidence_integrity"]["result"] == "pass"


def test_metrics_recomputed_fails_on_coordinated_score_row_tamper() -> None:
    """SEC-002 (coordinated-tamper evasion closed): editing BOTH ``clv_bps`` AND ``raw_action`` in a
    score row so the row is INTERNALLY CONSISTENT must STILL flip METRICS_RECOMPUTED to ``fail``.

    The tamper swaps a scored row to look like a WAIT (``raw_action`` = WAIT, ``clv_bps`` = the
    ``"pending"`` sentinel a WAIT recompute yields) — self-consistent, so a check that sourced the
    action from ``score_rows`` would be fooled into ``pass``. Because the action is instead read from
    the SEALED ``decision`` event (``action_payload_json``, the original FLAG_VALUE), the recompute
    still yields the real numeric clv ≠ ``"pending"`` ⇒ ``fail``. The sealed RunEvents are untouched,
    so ``evidence_integrity`` stays ``pass``.
    """
    run = finished_run_result()
    idx = _first_scored_index(run)
    row = run.score_rows[idx]
    # Coordinated, internally-consistent doctor of the NON-hashed score row:
    row["clv_bps"] = "pending"  # what a WAIT recompute would produce
    row["raw_prescore"]["raw_action"] = {"type": "WAIT", "params": {}}

    checks = _default_checks(score_run(run), run)

    assert checks["metrics_recomputed"]["result"] == "fail"
    assert checks["metrics_recomputed"]["error"] is not None
    # the recompute used the SEALED action (numeric clv), not the row's WAIT/"pending".
    assert any(rule.get("persisted_clv_bps") == "pending" for rule in checks["metrics_recomputed"]["rules"])
    assert checks["evidence_integrity"]["result"] == "pass"


def test_metrics_recomputed_not_applicable_when_no_score_rows() -> None:
    """A run with no score rows is an honest ``not_applicable`` (nothing to recompute), never a
    vacuous ``pass`` — matching the POLICY_OBEYED / RECEIPT_SEPARATION honesty pattern."""
    run = finished_run_result()
    run.score_rows.clear()  # mutate the list in place (frozen dataclass guards reassignment only)

    checks = _default_checks(score_run(run), run)
    assert checks["metrics_recomputed"]["result"] == "not_applicable"
