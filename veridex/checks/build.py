"""WD-5b — the Proof-Check builder: emit the 7 real CheckResults from sealed data.

Trust path (CON-007): NO LLM SDK import. ``RunResult`` is type-only (mirrors
``veridex.scoring``) so importing this module never drags the agent path into ``checks/``.

Each check is derived deterministically from sealed evidence — never a hardcoded PASS
(SEC-002), always fail-closed on error (CON-2B-02). The MANIFEST_BOUND / ANCHOR /
POLICY_OBEYED / RECEIPT_SEPARATION verdicts are filled by Tasks 3-4; Task 2 returns them
as ``not_applicable`` so the list is always length-7 and stably ordered.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from veridex.chain.anchor import run_manifest_hash
from veridex.checks.result import (
    CHECK_LABELS,
    CHECK_SEVERITY,
    CheckId,
    CheckResult,
)
from veridex.ingest.marketstate import MarketState
from veridex.law.recompute import recompute
from veridex.runtime.evidence import compute_evidence_hash, serialize_payload
from veridex.runtime.schemas import AgentAction
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


def _marketstates_from_events(run_events: list[dict[str, Any]]) -> list[MarketState]:
    """Reconstruct the ordered tick ``MarketState``s from the sealed RunEvent tick snapshots.

    Each ``tick`` RunEvent carries ``state_snapshot_json`` (the canonical dump of the snapshot the
    agents decided on). These are part of the hash-protected evidence prefix, so they are the
    tamper-resistant raw inputs the metric recompute re-derives CLV from. Ordered by ``tick_seq``.
    """
    states = [
        MarketState(**json.loads(e["state_snapshot_json"]))
        for e in run_events
        if e.get("event_type") == "tick" and e.get("state_snapshot_json")
    ]
    states.sort(key=lambda ms: ms.tick_seq)
    return states


def _closings_from_marketstates(marketstates: list[MarketState]) -> dict[str, MarketState]:
    """Map each ``market_key`` to its closing snapshot (the last tick carrying it).

    Mirrors the seal-time ``orchestrator._closing_snapshots`` (re-derived here rather than imported,
    so ``checks/`` keeps NO runtime dependency on the LLM agent shell).
    """
    closing_by_market: dict[str, MarketState] = {}
    for state in marketstates:  # ordered by tick_seq; later ticks overwrite earlier ones
        for market_key in state.markets:
            closing_by_market[market_key] = state
    return closing_by_market


def _actions_from_events(run_events: list[dict[str, Any]]) -> dict[tuple[Any, Any], dict[str, Any]]:
    """Map ``(tick_seq, agent_id) -> raw action dict``, sourced from the SEALED decision events.

    The agent action is read from the hash-protected ``action_payload_json`` on each ``decision``
    RunEvent (NOT from the non-hashed ``score_rows``), so a coordinated tamper of ``score_rows``
    (clv_bps + raw_action edited together) cannot evade the recompute. Decision events carry no
    ``tick_seq``, so it is derived from the preceding ``tick`` event: events are processed in
    ``sequence_no`` order, and a tick is followed by that tick's decisions until the next tick.
    The ``agent_id`` is read from the decision's ``result_payload_json``.
    """
    actions: dict[tuple[Any, Any], dict[str, Any]] = {}
    current_tick_seq: Any = None
    for e in sorted(run_events, key=lambda ev: ev.get("sequence_no", 0)):
        event_type = e.get("event_type")
        if event_type == "tick" and e.get("state_snapshot_json"):
            current_tick_seq = json.loads(e["state_snapshot_json"]).get("tick_seq")
        elif event_type == "decision" and e.get("action_payload_json"):
            agent_id = json.loads(e.get("result_payload_json") or "{}").get("agent_id")
            actions[(current_tick_seq, agent_id)] = json.loads(e["action_payload_json"])
    return actions


def _metrics_recomputed(scores: list[dict[str, Any]], run: RunResult) -> CheckResult:
    """Re-derive every persisted action metric from the SEALED evidence and confirm it matches.

    SEC-002 falsifiability: this check binds TWO INDEPENDENT sources so it can genuinely FAIL. Both
    recompute INPUTS are sourced from the hash-protected ``run_events`` (never from ``score_rows``):

      1. **Fresh recompute (from raw sealed evidence):** for each persisted ``score_rows`` entry,
         re-derive ``clv_bps`` by re-running the deterministic law :func:`~veridex.law.recompute.recompute`
         over the entry/closing ``MarketState``s reconstructed from the sealed ``tick`` snapshots AND
         the agent action read from the sealed ``decision`` event (``action_payload_json``) — NEVER
         from the already-stored clv NOR the row's own (non-hashed) ``raw_prescore.raw_action``.
      2. **Persisted/displayed metric:** the ``clv_bps`` stored on each ``score_rows`` entry (the value
         that aggregates into the leaderboard ``avg_clv_bps`` the judge reads).

    CLV is a performance metric, NOT a check (SEC-001); this check proves the displayed table was
    recomputed faithfully from sealed evidence. Because ``score_rows`` is NOT part of the hashed
    evidence prefix, tampering a displayed ``clv_bps`` is invisible to EVIDENCE_INTEGRITY and caught
    ONLY here: the fresh recompute diverges from the doctored row ⇒ ``fail`` (with the discrepancy in
    ``rules``). Sourcing the action from the sealed decision event also closes the coordinated-tamper
    evasion (editing ``clv_bps`` + ``raw_action`` together in a row). A run with no score rows is an
    honest ``not_applicable`` (nothing to recompute). Fail-closed on any recompute error (CON-2B-02).
    """
    # Honest not_applicable (mirrors POLICY_OBEYED / RECEIPT_SEPARATION): nothing to recompute.
    if not run.score_rows:
        return _not_applicable(CheckId.METRICS_RECOMPUTED, method="recompute_from_sealed_evidence", scope="score_rows")

    # Defend against an unexpected source_mode (fail-closed): anything other than "live" replays.
    source_mode: Literal["replay", "live"] = "live" if run.source_mode == "live" else "replay"
    try:
        marketstates = _marketstates_from_events(run.run_events)
        entry_by_tick: dict[Any, MarketState] = {ms.tick_seq: ms for ms in marketstates}
        closing_by_market = _closings_from_marketstates(marketstates)
        action_by_key = _actions_from_events(run.run_events)  # (tick_seq, agent_id) -> sealed raw action

        mismatches: list[dict[str, Any]] = []
        for row in run.score_rows:
            tick_seq = row.get("tick_seq")
            agent_id = row.get("agent_id")
            entry = entry_by_tick.get(tick_seq)
            raw_action = action_by_key.get((tick_seq, agent_id))
            if entry is None or raw_action is None:
                reason = "entry_snapshot_missing" if entry is None else "sealed_action_missing"
                mismatches.append({"tick_seq": tick_seq, "agent_id": agent_id, "reason": reason})
                continue
            action = AgentAction(**raw_action)
            market_key = (action.params or {}).get("market_key")
            closing = closing_by_market.get(market_key) if market_key else None
            redo = recompute(entry, action, closing=closing, source_mode=source_mode)
            if redo["clv_bps"] != row.get("clv_bps"):
                mismatches.append(
                    {
                        "agent_id": agent_id,
                        "tick_seq": tick_seq,
                        "persisted_clv_bps": row.get("clv_bps"),
                        "recomputed_clv_bps": redo["clv_bps"],
                    }
                )

        # Secondary guard (displayed-table vs persisted-rows, NOT an evidence guard): confirms the
        # aggregate metric table shown to the judge is a faithful aggregation of run.score_rows.
        aggregate_match = score_run(run) == scores
        if not aggregate_match:
            mismatches.append({"reason": "displayed_aggregate_diverges_from_score_rows"})

        match = not mismatches
        return _result(
            CheckId.METRICS_RECOMPUTED,
            "pass" if match else "fail",
            method="recompute_from_sealed_evidence",
            scope="score_rows",
            evidence_refs=["evidence_hash", "score_rows"],
            rules=mismatches if mismatches else [{"all_metrics_match": True}],
            details={"recomputed_match": match, "row_count": len(run.score_rows)},
            error=(None if match else f"{len(mismatches)} metric(s) diverged from the sealed-evidence recompute"),
        )
    except Exception as e:
        return _result(
            CheckId.METRICS_RECOMPUTED,
            "fail",
            method="recompute_from_sealed_evidence",
            scope="score_rows",
            details={"recomputed_match": False},
            error=f"{type(e).__name__}: {e}",
        )


def _manifest_bound(
    scores: list[dict[str, Any]],
    run: RunResult,
    manifest: dict[str, Any] | None,
    manifest_hash: str | None,
) -> CheckResult:
    """Verify the manifest binds the same run/evidence/score-root/lineage as the card.

    Fail-closed (CON-2B-02): an unserializable/malformed manifest (e.g. one that breaks the
    canonical hash) yields a ``fail`` verdict with a populated ``error`` — never a raised
    exception that would crash the whole check pass.
    """
    if manifest is None:
        return _not_applicable(CheckId.MANIFEST_BOUND, method="manifest_field_binding", scope="run_manifest")
    try:
        expected_score_root = hashlib.sha256(serialize_payload(scores).encode("utf-8")).hexdigest()
        mismatches: list[dict[str, Any]] = []
        checks: dict[str, bool] = {
            "run_id": manifest.get("run_id") == run.run_id,
            "action_evidence_root": manifest.get("action_evidence_root") == run.evidence_hash,
            "score_root": manifest.get("score_root") == expected_score_root,
            "proof_mode_map": manifest.get("proof_mode_map") == run.proof_mode_map,
            "code_prompt_schema_versions": bool(manifest.get("code_prompt_schema_versions")),
        }
        if manifest_hash is not None:
            checks["manifest_hash"] = run_manifest_hash(manifest) == manifest_hash
        for field, ok in checks.items():
            if not ok:
                mismatches.append({"field": field, "bound": False})
        return _result(
            CheckId.MANIFEST_BOUND,
            "pass" if not mismatches else "fail",
            method="manifest_field_binding",
            scope="run_manifest",
            evidence_refs=["evidence_hash", "score_root"],
            rules=mismatches if mismatches else [{"all_bound": True}],
            details={"bound_fields": sorted(checks)},
        )
    except Exception as e:  # unserializable/malformed manifest = binding FAIL, never a crash
        return _result(
            CheckId.MANIFEST_BOUND,
            "fail",
            method="manifest_field_binding",
            scope="run_manifest",
            evidence_refs=["evidence_hash", "score_root"],
            error=f"{type(e).__name__}: {e}",
        )


def _anchor(anchor: dict[str, Any] | None, source_mode: str | None) -> CheckResult:
    """Honest anchor verdict: anchored→pass, pending→pending, unanchored→na(replay)/pending(live)."""
    data = anchor or {}
    status = data.get("status", "not_anchored")
    if status == "anchored" and data.get("signature"):
        return _result(
            CheckId.ANCHOR,
            "pass",
            method="memo_anchor",
            scope="solana_memo",
            details={"signature": data["signature"], "cluster": data.get("cluster")},
        )
    if status == "pending":
        return _result(CheckId.ANCHOR, "pending", method="memo_anchor", scope="solana_memo")
    # not_anchored: pure offline replay is honestly not_applicable; live is awaiting its batch.
    result = "not_applicable" if source_mode == "replay" else "pending"
    return _result(CheckId.ANCHOR, result, method="memo_anchor", scope="solana_memo")


def _not_applicable(check_id: CheckId, *, method: str, scope: str) -> CheckResult:
    """A placeholder verdict for a check whose inputs are absent (honest not_applicable)."""
    return _result(check_id, "not_applicable", method=method, scope=scope)


#: The derived (``evidence=False``) executor-lane event types RECEIPT_SEPARATION audits.
_EXEC_EVENT_TYPES = {"policy_result", "execution_submitted", "execution_receipt", "approval_audit"}


def _execution_id_from_derived(derived_from: list[str]) -> str | None:
    """Pull the execution-record id from a derived_from ref like ``execution_record:{id}``."""
    for ref in derived_from:
        if ref.startswith("execution_record:"):
            return ref.split(":", 1)[1]
    return None


def _policy_obeyed(events: list[dict[str, Any]] | None) -> CheckResult:
    """Assert no DENIED policy result was followed by a submit for the same execution.

    POLICY_OBEYED correlates a ``denied`` ``policy_result`` (by its ``payload.execution_id``,
    threaded by the executor lane) with any ``execution_submitted`` for that execution record.
    Honest ``not_applicable`` (no policy events) stays OUTSIDE the try; a present-but-malformed
    event stream fails closed (CON-2B-02) rather than crashing the whole check pass.
    """
    if not events or not any(e.get("event_type") == "policy_result" for e in events):
        return _not_applicable(CheckId.POLICY_OBEYED, method="policy_event_audit", scope="competition_events")
    try:
        denied_exec_ids = {
            e.get("payload", {}).get("execution_id")
            for e in events
            if e.get("event_type") == "policy_result" and e.get("payload", {}).get("decision") == "denied"
        }
        denied_exec_ids.discard(None)
        submitted_exec_ids = {
            _execution_id_from_derived(e.get("derived_from", []))
            for e in events
            if e.get("event_type") == "execution_submitted"
        }
        submitted_exec_ids.discard(None)
        bypassed = sorted(denied_exec_ids & submitted_exec_ids)
        return _result(
            CheckId.POLICY_OBEYED,
            "pass" if not bypassed else "fail",
            method="policy_event_audit",
            scope="competition_events",
            rules=[{"bypassed_execution_id": eid} for eid in bypassed] or [{"no_bypass": True}],
            details={"denied_count": len(denied_exec_ids)},
        )
    except Exception as e:  # a malformed/unscannable event stream fails closed, never crashes
        return _result(
            CheckId.POLICY_OBEYED,
            "fail",
            method="policy_event_audit",
            scope="competition_events",
            error=f"{type(e).__name__}: {e}",
        )


def _receipt_separation(events: list[dict[str, Any]] | None) -> CheckResult:
    """Assert every policy/execution event stayed ``evidence=False`` (the SEC-004 invariant).

    Execution receipts/fills are production-readiness evidence only: any one that leaked into
    the sealed evidence prefix (``evidence is True``) is a ``fail``. Honest ``not_applicable``
    (no executor-lane events) stays OUTSIDE the try; a malformed stream fails closed.
    """
    if not events or not any(e.get("event_type") in _EXEC_EVENT_TYPES for e in events):
        return _not_applicable(CheckId.RECEIPT_SEPARATION, method="evidence_flag_audit", scope="competition_events")
    try:
        exec_events = [e for e in events if e.get("event_type") in _EXEC_EVENT_TYPES]
        leaked = [e for e in exec_events if e.get("evidence") is True]
        return _result(
            CheckId.RECEIPT_SEPARATION,
            "pass" if not leaked else "fail",
            method="evidence_flag_audit",
            scope="competition_events",
            rules=[{"leaked_event_type": e.get("event_type")} for e in leaked] or [{"all_derived": True}],
            details={"execution_event_count": len(exec_events)},
        )
    except Exception as e:  # a malformed/unscannable event stream fails closed, never crashes
        return _result(
            CheckId.RECEIPT_SEPARATION,
            "fail",
            method="evidence_flag_audit",
            scope="competition_events",
            error=f"{type(e).__name__}: {e}",
        )


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
        _manifest_bound(scores, run, manifest, manifest_hash),
        _policy_obeyed(events),
        _receipt_separation(events),
        _anchor(anchor, source_mode),
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
