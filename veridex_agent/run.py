"""WD-3 — the decoupled standalone-run core (REQ-052).

Runs ONE agent through the full Veridex spine — ingest → agent → law → evidence/seal → proof →
self-verify → (optional) anchor — with NO competition/roster/ranking framing. It composes the
lower-level pieces directly (``run_competition`` for a single agent, ``score_run``, the shared
manifest/score-root helpers, ``proof_card``, ``verify_run``) so the same core can later back an
in-app "Deployment" surface without the competition container.

Same trust boundary as the arena: the law recomputes the score, the proof card binds the sealed
evidence hash, and the anchored Memo payload == the manifest hash. Credentials (Solana keypair
for the real ``anchor_memo``) are resolved from typed config inside the anchor seam — never here.
This module is an agent SHELL (it may transitively reach the LLM agent path via the orchestrator);
it is NOT import-audited as trust-path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from veridex.chain.anchor import anchor_memo, run_manifest_hash
from veridex.checks.build import build_check_results, build_performance_metrics, check_results_to_proof_block
from veridex.ingest.marketstate import MarketState
from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime.competition import DEFAULT_CLUSTER, SCHEMA_VERSIONS
from veridex.runtime.orchestrator import Agent, run_competition
from veridex.scoring import score_run
from veridex.verifier.proof_card import proof_card_from_run_result
from veridex.verifier.recompute import (
    fixture_or_window_id_from_events,
    manifest_from_run,
    recompute_score_root,
    verify_run,
)


class StandaloneRunResult(BaseModel):
    """The full artifact bundle of one standalone agent run (WD-3).

    Attributes:
        run_id: The sealed run identifier.
        source_mode: ``"replay"`` or ``"live"``.
        scores: The single-agent ranked metric stack (one row).
        proof_card: The judge-visible proof card (lineage + checks + anchor).
        manifest_hash: SHA-256 of the run manifest (the exact anchored Memo payload).
        anchor_status: ``"anchored"`` or ``"not_anchored"``.
        signature: The Solana tx signature when anchored, else ``None``.
        verified: The self-verify verdict over the produced proof.
        verify_report: The :class:`~veridex.verifier.recompute.VerifyReport` as a dict.
    """

    run_id: str
    source_mode: str
    scores: list[dict[str, Any]]
    proof_card: dict[str, Any]
    manifest_hash: str
    anchor_status: str
    signature: str | None
    verified: bool
    verify_report: dict[str, Any]


async def standalone_run(
    marketstates: list[MarketState],
    agent: Agent,
    *,
    source_mode: str = "replay",
    policy_envelope: PolicyEnvelope | None = None,  # noqa: ARG001  (reserved for the opt-in execution lane)
    anchor_fn: Callable[[str], Awaitable[str]] | None = anchor_memo,
) -> StandaloneRunResult:
    """Run ONE agent through the full spine and return a self-verified, optionally-anchored proof.

    Args:
        marketstates: Ordered tick snapshots (replay fixture or live stream batch).
        agent: The single :class:`~veridex.runtime.orchestrator.Agent` to run.
        source_mode: ``"replay"`` or ``"live"``.
        policy_envelope: Reserved for the opt-in policy-gated execution lane (paper by default);
            unused in the minimal proof-only core.
        anchor_fn: Injectable ``async (manifest_hash) -> signature``; ``None`` skips anchoring
            (``anchor_status="not_anchored"``). Defaults to the real
            :func:`~veridex.chain.anchor.anchor_memo` (Solana creds resolved inside the seam).

    Returns:
        The :class:`StandaloneRunResult` bundle.
    """
    run = await run_competition(marketstates, [agent], source_mode=source_mode)
    scores = score_run(run)

    score_root = recompute_score_root(scores)
    manifest = manifest_from_run(
        run,
        fixture_or_window_id=fixture_or_window_id_from_events(run.run_events),
        score_root=score_root,
        schema_versions=dict(SCHEMA_VERSIONS),
    )
    manifest_hash = run_manifest_hash(manifest)

    if anchor_fn is None:
        anchor_status = "not_anchored"
        signature: str | None = None
    else:
        signature = await anchor_fn(manifest_hash)
        anchor_status = "anchored"

    # Plan A's split (SEC-001): the 7-CheckId proof block (clv excluded) + a separate metrics
    # block (clv lives here) — the SAME builders the arena proof card / verify endpoint use, so a
    # standalone-deployed agent's proof reads identically to an in-competition one. (build_check_results
    # is keyword-only; build_performance_metrics takes scores only — matching the landed signatures.)
    checks = check_results_to_proof_block(build_check_results(scores=scores, run=run))
    metrics = build_performance_metrics(scores)
    anchor_block = {"status": anchor_status, "signature": signature, "cluster": DEFAULT_CLUSTER}
    proof_card = proof_card_from_run_result(
        run, checks=checks, metrics=metrics, anchor=anchor_block, schema_versions=dict(SCHEMA_VERSIONS)
    )

    report = verify_run(run)

    return StandaloneRunResult(
        run_id=run.run_id,
        source_mode=source_mode,
        scores=scores,
        proof_card=proof_card,
        manifest_hash=manifest_hash,
        anchor_status=anchor_status,
        signature=signature,
        verified=report.verified,
        verify_report=report.model_dump(),
    )
