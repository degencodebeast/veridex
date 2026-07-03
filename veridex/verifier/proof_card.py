"""Proof card — verifier-run-response-shaped JSON (read-only, JSON/static; NO UI). Test-driven (T7, B8).

The judge-visible artifact. An earlier internal run-model named its checks block with a legacy
internal name; the PUBLIC proof card must surface it as ``checks`` / Proof Checks via a thin
response adapter (KILL-6 if that needs broad schema rewrites). Must never expose that legacy
internal name in any public JSON field name (AC-111 / KILL-6).

Phase 1 B8 enriches the Phase-0 card with ``verifier_version``, ``lineage`` (proof-mode map +
schema versions), and ``anchor`` status. The ``proof_card_from_run_result`` helper builds the
card directly from a ``RunResult`` produced by ``veridex.runtime.orchestrator.run_competition``.
B8 does NOT perform anchoring — that is B9.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from veridex.runtime.orchestrator import RunResult

#: Default verifier version string embedded in every proof card.
VERIFIER_VERSION: str = "v0"

#: Default schema-version map embedded in ``lineage.schema_versions`` when none is supplied.
DEFAULT_SCHEMA_VERSIONS: dict[str, str] = {
    "action_schema": "sports_v0",
    "verifier": "v0",
}


def build_proof_card(
    *,
    run: dict[str, Any],
    evidence: dict[str, Any],
    checks: dict[str, Any],
    proof_mode: str,
    verifier_version: str = VERIFIER_VERSION,
    proof_mode_map: dict[str, str] | None = None,
    schema_versions: dict[str, str] | None = None,
    anchor: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the public proof-card JSON.

    Produces the judge-visible dict with ``verifier_version``, ``run``, ``lineage``,
    ``evidence``, ``checks``, and ``anchor`` (plus an optional ``metrics`` block).  The key
    The legacy internal name never appears anywhere in the returned structure (KILL-6 / AC-111).

    Backward-compatible with the Phase-0 signature: existing callers that pass only
    ``run``, ``evidence``, ``checks``, and ``proof_mode`` receive a fully-enriched card
    with sensible defaults for the B8 additions.

    Args:
        run: Run metadata dict (e.g. ``{"run_id": ..., "source_mode": ...}``).
        evidence: Evidence block (e.g. ``{"evidence_hash": ..., "run_event_count": ...}``).
        checks: Proof-Checks mapping — always exposed as ``checks``, never a legacy internal name.
        proof_mode: Proof-mode label for the run (``"reproducible"`` or
            ``"LLM/evidence-verified"``); used to synthesise a default ``proof_mode_map``
            when none is provided.
        verifier_version: Verifier version string embedded in the card (default ``"v0"``).
        proof_mode_map: Explicit agent→proof-mode map placed in ``lineage``; when
            ``None`` a single-entry map keyed on ``str(run["run_id"])`` is synthesised
            from ``proof_mode``.
        schema_versions: Schema-version map placed in ``lineage``; defaults to
            ``{"action_schema": "sports_v0", "verifier": "v0"}``.
        anchor: Anchor-status block; defaults to
            ``{"status": "not_anchored", "signature": None, "cluster": None}``.
            B8 does NOT perform anchoring — that is B9.
        metrics: Optional Performance-Metrics block (SEC-001 — CLV + all metrics live here,
            never in ``checks``). When ``None`` the ``metrics`` key is omitted (back-compat).

    Returns:
        A public proof-card dict with the keys ``verifier_version``, ``run``,
        ``lineage``, ``evidence``, ``checks``, and ``anchor`` (and ``metrics`` when supplied).
    """
    resolved_proof_mode_map: dict[str, str]
    if proof_mode_map is not None:
        resolved_proof_mode_map = proof_mode_map
    else:
        run_id = str(run.get("run_id", "unknown"))
        resolved_proof_mode_map = {run_id: proof_mode}

    resolved_schema_versions: dict[str, str] = (
        schema_versions if schema_versions is not None else dict(DEFAULT_SCHEMA_VERSIONS)
    )

    resolved_anchor: dict[str, Any] = (
        anchor if anchor is not None else {"status": "not_anchored", "signature": None, "cluster": None}
    )

    card: dict[str, Any] = {
        "verifier_version": verifier_version,
        "run": run,
        "lineage": {
            "proof_mode_map": resolved_proof_mode_map,
            "schema_versions": resolved_schema_versions,
        },
        "evidence": evidence,
        "checks": checks,
        "anchor": resolved_anchor,
    }
    if metrics is not None:
        card["metrics"] = metrics  # Performance Metrics — separate from checks (SEC-001)
    return card


def proof_card_from_run_result(
    run_result: RunResult,
    *,
    checks: dict[str, Any],
    anchor: dict[str, Any] | None = None,
    verifier_version: str = VERIFIER_VERSION,
    schema_versions: dict[str, str] | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a proof card directly from a ``RunResult``.

    Convenience builder that extracts ``run_id``, ``source_mode``, ``evidence_hash``,
    ``run_events``, and ``proof_mode_map`` from the run result and delegates to
    :func:`build_proof_card`.  ``checks`` must be supplied by the caller (the run
    result carries score rows, not pre-aggregated checks).

    Args:
        run_result: A :class:`~veridex.runtime.orchestrator.RunResult` from
            :func:`~veridex.runtime.orchestrator.run_competition`.
        checks: Proof-Checks mapping to embed in the card (PUBLIC name — never a legacy internal name).
        anchor: Anchor-status block supplied by B9; defaults to ``"not_anchored"``.
        verifier_version: Verifier version string (default ``"v0"``).
        schema_versions: Schema-version map for ``lineage``; defaults to
            ``{"action_schema": "sports_v0", "verifier": "v0"}``.
        metrics: Optional Performance-Metrics block (SEC-001); ``None`` ⇒ ``metrics`` key omitted.

    Returns:
        A public proof-card dict with ``lineage.proof_mode_map`` and
        ``evidence.evidence_hash`` sourced directly from the run result.
    """
    proof_mode = next(iter(run_result.proof_mode_map.values()), "reproducible")

    return build_proof_card(
        run={
            "run_id": run_result.run_id,
            "source_mode": run_result.source_mode,
        },
        evidence={
            "evidence_hash": run_result.evidence_hash,
            "run_event_count": len(run_result.run_events),
        },
        checks=checks,
        proof_mode=proof_mode,
        verifier_version=verifier_version,
        proof_mode_map=run_result.proof_mode_map,
        schema_versions=schema_versions,
        anchor=anchor,
        metrics=metrics,
    )
