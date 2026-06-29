"""WD-5b — the Proof-Check builder: emit the 7 real CheckResults from sealed data.

Trust path (CON-007): NO LLM SDK import. ``RunResult`` is type-only (mirrors
``veridex.scoring``) so importing this module never drags the agent path into ``checks/``.

Each check is derived deterministically from sealed evidence — never a hardcoded PASS
(SEC-002), always fail-closed on error (CON-2B-02). The MANIFEST_BOUND / ANCHOR /
POLICY_OBEYED / RECEIPT_SEPARATION verdicts are filled by Tasks 3-4; Task 2 returns them
as ``not_applicable`` so the list is always length-7 and stably ordered.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from veridex.checks.result import (
    CHECK_LABELS,
    CHECK_SEVERITY,
    CheckId,
    CheckResult,
)
from veridex.runtime.evidence import compute_evidence_hash
from veridex.scoring import score_run
from veridex.verifier.import_audit import assert_no_llm_imports

if TYPE_CHECKING:  # type-only; keeps checks/ free of a runtime dependency on the agent path
    from veridex.runtime.orchestrator import RunResult

_VERIDEX_PKG: Path = Path(__file__).parent.parent

#: The exact trust-path targets the LLM_BOUNDARY audit sweeps (WD-5a parity).
_TRUST_TARGETS: tuple[Path, ...] = (
    _VERIDEX_PKG / "law",
    _VERIDEX_PKG / "scoring.py",
    _VERIDEX_PKG / "leaderboard.py",
    _VERIDEX_PKG / "verifier",
    _VERIDEX_PKG / "checks",
    _VERIDEX_PKG / "ingest",
    _VERIDEX_PKG / "policy",
)
_TRUST_SCOPE = "law/, scoring.py, leaderboard.py, verifier/, checks/, ingest/, policy/"


def _result(check_id: CheckId, result: str, *, method: str, scope: str, **kw: Any) -> CheckResult:
    """Construct a ``CheckResult`` with the frozen label/severity for ``check_id``."""
    return CheckResult(
        id=check_id,
        label=CHECK_LABELS[check_id],
        result=result,  # type: ignore[arg-type]  # validated by the Literal
        severity=CHECK_SEVERITY[check_id],
        method=method,
        scope=scope,
        evidence_refs=kw.get("evidence_refs", []),
        rules=kw.get("rules", []),
        details=kw.get("details", {}),
        error=kw.get("error"),
    )


def _evidence_integrity(run: RunResult) -> CheckResult:
    """Recompute the evidence hash and compare; fail-closed on any recompute error (WD-5a)."""
    try:
        recomputed = compute_evidence_hash(run.run_events)
        match = recomputed == run.evidence_hash
        return _result(
            CheckId.EVIDENCE_INTEGRITY,
            "pass" if match else "fail",
            method="sha256_evidence_hash",
            scope="run_events",
            evidence_refs=["evidence_hash"],
            details={"recomputed_match": match},
        )
    except Exception as e:  # malformed/unrecomputable evidence = integrity FAIL, never a crash
        return _result(
            CheckId.EVIDENCE_INTEGRITY,
            "fail",
            method="sha256_evidence_hash",
            scope="run_events",
            details={"recomputed_match": False},
            error=f"{type(e).__name__}: {e}",
        )


def _llm_boundary() -> CheckResult:
    """Run the real static import audit over every trust-path target; fail-closed (WD-5a)."""
    try:
        for target in _TRUST_TARGETS:
            assert_no_llm_imports(target)
        return _result(CheckId.LLM_BOUNDARY, "pass", method="static_import_audit", scope=_TRUST_SCOPE)
    except Exception as e:  # AssertionError (violation) or SyntaxError/OSError (unreadable) → fail
        return _result(
            CheckId.LLM_BOUNDARY,
            "fail",
            method="static_import_audit",
            scope=_TRUST_SCOPE,
            error=f"{type(e).__name__}: {e}",
        )


def _metrics_recomputed(scores: list[dict[str, Any]], run: RunResult) -> CheckResult:
    """Recompute the metric table from sealed evidence and confirm it matches the visible table.

    CLV itself is a performance metric, NOT a check (SEC-001); this check proves the table the
    judge reads was recomputed faithfully from the sealed ``RunResult`` (Checks Doctrine).
    """
    try:
        recomputed = score_run(run)
        match = recomputed == scores
        return _result(
            CheckId.METRICS_RECOMPUTED,
            "pass" if match else "fail",
            method="recompute_score_run",
            scope="score_rows",
            evidence_refs=["evidence_hash"],
            details={"recomputed_match": match, "row_count": len(recomputed)},
        )
    except Exception as e:
        return _result(
            CheckId.METRICS_RECOMPUTED,
            "fail",
            method="recompute_score_run",
            scope="score_rows",
            details={"recomputed_match": False},
            error=f"{type(e).__name__}: {e}",
        )


def _not_applicable(check_id: CheckId, *, method: str, scope: str) -> CheckResult:
    """A placeholder verdict for a check whose inputs are absent (honest not_applicable)."""
    return _result(check_id, "not_applicable", method=method, scope=scope)


def build_check_results(
    *,
    scores: list[dict[str, Any]],
    run: RunResult,
    manifest: dict[str, Any] | None = None,
    manifest_hash: str | None = None,
    anchor: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    source_mode: str | None = None,
) -> list[CheckResult]:
    """Build all 7 Proof Checks from sealed data, in ``CheckId`` declaration order.

    Args:
        scores: The ranked metric stack (``score_run`` output) shown on the card.
        run: The sealed run (evidence hash + events + score rows).
        manifest / manifest_hash / anchor: Filled in by Task 3 (MANIFEST_BOUND / ANCHOR);
            ``None`` ⇒ those checks are ``not_applicable`` (manifest) / per-source-mode (anchor).
        events: Derived competition events (Task 4 POLICY_OBEYED / RECEIPT_SEPARATION);
            ``None`` ⇒ those checks are ``not_applicable``.
        source_mode: ``"replay"`` / ``"live"`` — governs the offline ANCHOR verdict.

    Returns:
        Exactly 7 ``CheckResult`` objects, one per ``CheckId``.
    """
    return [
        _evidence_integrity(run),
        _llm_boundary(),
        _metrics_recomputed(scores, run),
        _not_applicable(CheckId.MANIFEST_BOUND, method="manifest_field_binding", scope="run_manifest"),
        _not_applicable(CheckId.POLICY_OBEYED, method="policy_event_audit", scope="competition_events"),
        _not_applicable(CheckId.RECEIPT_SEPARATION, method="evidence_flag_audit", scope="competition_events"),
        _not_applicable(CheckId.ANCHOR, method="memo_anchor", scope="solana_memo"),
    ]


def check_results_to_proof_block(results: list[CheckResult]) -> dict[str, dict[str, Any]]:
    """Serialize the check list into the proof-card ``checks`` block, keyed by ``CheckId`` value."""
    return {r.id.value: r.model_dump(mode="json") for r in results}


def build_performance_metrics(scores: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the separate Performance-Metrics block (SEC-001 — CLV lives here, NOT in checks).

    Args:
        scores: The ranked metric stack (``score_run`` output).

    Returns:
        ``{clv, sim_pnl, brier, hit_rate, max_drawdown, per_agent}`` — rank-1 headline values
        plus a per-agent breakdown. ``clv`` is the rank-1 ``avg_clv_bps`` (``None`` if unscored).
    """
    top = scores[0] if scores else {}
    total_actions = sum(int(r.get("action_count", 0)) for r in scores)
    return {
        "clv": top.get("avg_clv_bps"),
        "sim_pnl": top.get("sim_pnl"),
        "brier": top.get("brier"),
        "max_drawdown": top.get("max_drawdown"),
        "hit_rate": _hit_rate(scores),
        "scored_actions": total_actions,
        "per_agent": [
            {
                "agent_id": r.get("agent_id"),
                "rank": r.get("rank"),
                "avg_clv_bps": r.get("avg_clv_bps"),
                "total_clv_bps": r.get("total_clv_bps"),
                "sim_pnl": r.get("sim_pnl"),
                "brier": r.get("brier"),
                "max_drawdown": r.get("max_drawdown"),
                "action_count": r.get("action_count"),
            }
            for r in scores
        ],
    }


def _hit_rate(scores: list[dict[str, Any]]) -> float | None:
    """Fraction of scored agents with a positive avg CLV (closing-line confirmation proxy)."""
    scored = [r for r in scores if isinstance(r.get("avg_clv_bps"), (int, float))]
    if not scored:
        return None
    wins = sum(1 for r in scored if r["avg_clv_bps"] > 0)
    return wins / len(scored)
