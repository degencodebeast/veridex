"""WD-1 — authoritative verify/recompute over a SEALED run (CON-003, REQ-050 / AC-020).

TRUST PATH (CON-004): this module MUST NOT import an LLM SDK (agno/anthropic/openai/
google.generativeai/litellm). It re-executes the deterministic spine over the sealed
``RunResult`` only — recomputing the evidence hash, the ranked score rows, the score root,
the per-domain root forest, and the run manifest — so a judge can independently confirm what
the Proof Card asserts. It never reads execution receipts (SEC-004: receipt-independence is by
construction).

This module is also the SINGLE SOURCE OF TRUTH for the score-root, root-forest, and manifest
formulas: ``veridex.runtime.competition`` is refactored (Task D2) to build its anchored manifest
through :func:`manifest_from_run` + :func:`root_forest_for_run`, so the manifest this module
reconstructs hashes byte-identically to the one that was anchored.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from veridex.chain.anchor import run_manifest, run_manifest_hash
from veridex.runtime.evidence import compute_evidence_hash, serialize_payload
from veridex.scoring import score_run
from veridex.verifier.proof_card import DEFAULT_SCHEMA_VERSIONS

if TYPE_CHECKING:  # type-only — keeps the trust path import-light and cycle-free.
    from veridex.runtime.orchestrator import RunResult


def recompute_score_root(scores: list[dict[str, Any]]) -> str:
    """SHA-256 over the canonically-serialized ranked score rows.

    Mirrors the manifest score-root formula so a recomputed root is comparable to the anchored
    one byte-for-byte.

    Args:
        scores: The :func:`veridex.scoring.score_run` output.

    Returns:
        A 64-character hex digest.
    """
    return hashlib.sha256(serialize_payload(scores).encode("utf-8")).hexdigest()


def fixture_or_window_id_from_events(run_events: list[dict[str, Any]]) -> str:
    """Derive the manifest ``fixture_or_window_id`` from the first sealed tick event.

    Walks ``run_events`` in ``sequence_no`` order and reads ``fixture_id`` from the first
    ``tick`` event's ``state_snapshot_json``. Falls back to ``"unknown"`` (matching the harness)
    when no tick carries a fixture id.

    Args:
        run_events: The sealed, ``RunEvent``-validated event dicts.

    Returns:
        The fixture/window identifier as a string.
    """
    for event in sorted(run_events, key=lambda e: e["sequence_no"]):
        snapshot_json = event.get("state_snapshot_json")
        if not snapshot_json:
            continue
        try:
            snapshot = json.loads(snapshot_json)
        except (ValueError, TypeError):
            continue
        fixture_id = snapshot.get("fixture_id")
        if fixture_id is not None:
            return str(fixture_id)
    return "unknown"


def manifest_from_run(
    run_result: RunResult,
    *,
    fixture_or_window_id: str,
    score_root: str,
    schema_versions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the base run manifest dict from a sealed run + precomputed score root.

    Pure: never re-scores (the caller supplies ``score_root``) and never performs I/O. Returns the
    *base* manifest (without the ``root_forest`` block — that is attached by
    :func:`root_forest_for_run`, which both the verifier and the harness call, so the two complete
    manifests agree byte-for-byte).

    Args:
        run_result: The sealed run.
        fixture_or_window_id: The committed fixture/window identifier.
        score_root: The SHA-256 score root from :func:`recompute_score_root`.
        schema_versions: Code/prompt/schema versions; defaults to ``DEFAULT_SCHEMA_VERSIONS``.

    Returns:
        The base manifest dict (pre-``root_forest``).
    """
    return run_manifest(
        run_id=run_result.run_id,
        fixture_or_window_id=fixture_or_window_id,
        agent_ids=run_result.agent_ids,
        action_evidence_root=run_result.evidence_hash,
        score_root=score_root,
        proof_mode_map=run_result.proof_mode_map,
        code_prompt_schema_versions=dict(schema_versions if schema_versions is not None else DEFAULT_SCHEMA_VERSIONS),
    )


def root_forest_for_run(run_result: RunResult, scores: list[dict[str, Any]]) -> dict[str, str]:
    """Recompute the per-domain Merkle root forest for the sealed run (manifest ``root_forest``).

    The SINGLE source of the root-forest formula: reconstructs the exact ``build_root_forest`` call
    the harness made at seal time from the sealed ``RunResult`` only — the event log + recomputed
    score rows + the competition meta. The demo path runs no executor lane, so ``receipts`` and
    ``policy_results`` are empty (SEC-004: receipts never enter the manifest binding).

    Args:
        run_result: The sealed run.
        scores: The recomputed ranked score rows from :func:`veridex.scoring.score_run`.

    Returns:
        The ``root_forest`` block (domain → hex root) attached to the manifest before hashing.
    """
    from veridex.chain.merkle import build_root_forest  # local import keeps the trust core load light

    return build_root_forest(
        event_log=run_result.run_events,
        score_rows=scores,
        receipts=[],  # demo path runs no executor lane
        policy_results=[],
        competition=[
            {
                "run_id": run_result.run_id,
                "source_mode": run_result.source_mode,
                "agent_ids": run_result.agent_ids,
            }
        ],
    )


class VerifyReport(BaseModel):
    """The authoritative recompute result a judge's "Verify" action renders.

    Attributes:
        run_id: The verified run identifier.
        source_mode: ``"replay"`` or ``"live"`` carried from the run.
        evidence_hash_sealed: The hash stored on the sealed run.
        evidence_hash_recomputed: The hash recomputed over ``run_events`` (``None`` on error).
        evidence_match: Whether recomputed == sealed (fail-closed on any recompute error).
        evidence_error: The recompute error string, when the hash could not be recomputed.
        score_root: SHA-256 over the recomputed ranked score rows.
        score_rows: The recomputed ranked per-agent metric stack.
        manifest: The reconstructed run manifest (base + ``root_forest``).
        manifest_hash: SHA-256 of ``manifest`` (the exact anchored Memo payload).
        verified: Overall verdict — ``True`` iff the evidence hash matched.
    """

    run_id: str
    source_mode: str
    evidence_hash_sealed: str
    evidence_hash_recomputed: str | None
    evidence_match: bool
    evidence_error: str | None
    score_root: str
    score_rows: list[dict[str, Any]]
    manifest: dict[str, Any]
    manifest_hash: str
    verified: bool


def verify_run(run_result: RunResult, *, fixture_or_window_id: str | None = None) -> VerifyReport:
    """Independently recompute the law/scoring/manifest from a sealed run (WD-1, CON-003).

    Recomputes the evidence hash over the sealed ``run_events`` prefix and compares it to the
    sealed hash (fail-closed on any recompute error — a tampered or malformed run is an integrity
    failure, never a crash): tampering ANY sealed event/snapshot/action payload diverges the hash
    and flips ``verified`` to ``False``. It then re-derives the ranked score stack, the score root,
    the root forest, and the run manifest, and hashes the manifest to the exact anchored Memo
    payload. Reads ONLY the sealed run — never receipts (SEC-004) and never mutates the seal.

    Args:
        run_result: The sealed run to verify.
        fixture_or_window_id: Optional explicit fixture id; defaults to the value derived from
            the first sealed tick event (matching how the harness built the anchored manifest).

    Returns:
        The :class:`VerifyReport` the Proof Card "Verify" renders.
    """
    sealed = run_result.evidence_hash
    try:
        recomputed: str | None = compute_evidence_hash(run_result.run_events)
        evidence_match = recomputed == sealed
        evidence_error: str | None = None
    except Exception as exc:  # dup/missing sequence_no or malformed evidence → integrity FAIL
        recomputed = None
        evidence_match = False
        evidence_error = f"{type(exc).__name__}: {exc}"

    scores = score_run(run_result)
    score_root = recompute_score_root(scores)
    resolved_fixture_id = (
        fixture_or_window_id
        if fixture_or_window_id is not None
        else fixture_or_window_id_from_events(run_result.run_events)
    )
    manifest = manifest_from_run(run_result, fixture_or_window_id=resolved_fixture_id, score_root=score_root)
    # Bind the per-domain root forest identically to seal time so the reconstructed manifest_hash
    # stays byte-identical to the anchored one (Task D2 routes the harness through the same helper).
    manifest["root_forest"] = root_forest_for_run(run_result, scores)
    manifest_hash = run_manifest_hash(manifest)

    return VerifyReport(
        run_id=run_result.run_id,
        source_mode=run_result.source_mode,
        evidence_hash_sealed=sealed,
        evidence_hash_recomputed=recomputed,
        evidence_match=evidence_match,
        evidence_error=evidence_error,
        score_root=score_root,
        score_rows=scores,
        manifest=manifest,
        manifest_hash=manifest_hash,
        verified=bool(evidence_match),
    )
