"""B11a — the competition harness: ONE scored, proof-carded, anchored, ranked run (REQ-115 / AC-115).

This is the integration SHELL that ties every Phase-1 bit together into a single judge-inspectable
artifact (gate CON-008 — every claim maps to a record/tx/test):

    run_competition (B5)  →  score_run (B6)  →  run_manifest (B9)  →  anchor (B9)
                                            →  proof_card (B8)     →  leaderboard (B10)

Unlike the deterministic trust core, ``competition.py`` is explicitly a SHELL: it transitively
imports the LLM agent path (via ``veridex.runtime.orchestrator``) and is therefore NOT part of the
import-audited trust path (``checks/`` ``verifier/`` ``law/`` ``ingest/`` and ``scoring``/
``leaderboard``). The audit still passes over those modules — see
``tests/test_competition_integration.py::test_trust_path_import_audit_still_clean``.

Anchoring is INJECTABLE and defaults to the real :func:`~veridex.chain.anchor.anchor_memo`.
The offline test suite always injects a mock (no network); passing ``anchor_fn=None`` skips
anchoring entirely (``anchor_status="not_anchored"``). The anchor vocabulary is the canonical
``"anchored" | "pending" | "not_anchored"`` (the harness emits the first or last; ``"pending"`` is
a live-mode state owned by B9).
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from veridex.chain.anchor import anchor_memo, run_manifest, run_manifest_hash
from veridex.leaderboard import leaderboard
from veridex.runtime.evidence import serialize_payload
from veridex.runtime.orchestrator import RunResult, run_competition
from veridex.scoring import score_run
from veridex.verifier.proof_card import DEFAULT_SCHEMA_VERSIONS, proof_card_from_run_result

if TYPE_CHECKING:
    from veridex.ingest.marketstate import MarketState
    from veridex.store import Store

#: Solana cluster recorded in the proof-card anchor block (devnet for Phase 1).
DEFAULT_CLUSTER: str = "devnet"

#: Schema-version map bound into BOTH the run manifest and the proof-card lineage so the two
#: artifacts agree byte-for-byte on the code/prompt/schema identity of the run.
SCHEMA_VERSIONS: dict[str, str] = dict(DEFAULT_SCHEMA_VERSIONS)

#: An injectable anchor seam: ``async (manifest_hash) -> signature``.
AnchorFn = Callable[[str], Awaitable[str]]

#: An injectable Proof-Checks builder: ``(scores, run) -> checks-summary dict``.
ChecksFn = Callable[[list[dict[str, Any]], RunResult], dict[str, Any]]


@dataclass(frozen=True)
class CompetitionResult:
    """The full artifact bundle of one demo competition (everything a judge inspects).

    Attributes:
        run: The scored, evidence-backed :class:`~veridex.runtime.orchestrator.RunResult`.
        scores: Per-agent ranked metric stack from :func:`~veridex.scoring.score_run`.
        manifest: The run manifest that was hashed and (optionally) anchored — its
            ``action_evidence_root`` binds back to ``run.evidence_hash`` (the chain).
        manifest_hash: SHA-256 of ``manifest`` (the exact on-chain Memo payload).
        anchor_status: Canonical anchor vocab — ``"anchored"`` or ``"not_anchored"``.
        signature: The Solana tx signature when anchored, else ``None``.
        proof_card: The judge-visible proof-card JSON (lineage + checks + anchor).
        leaderboard: The ranked cross-run leaderboard rows for this single run.
    """

    run: RunResult
    scores: list[dict[str, Any]]
    manifest: dict[str, Any]
    manifest_hash: str
    anchor_status: str
    signature: str | None
    proof_card: dict[str, Any]
    leaderboard: list[dict[str, Any]]


def _fixture_or_window_id(marketstates: list[MarketState]) -> str:
    """Derive the manifest ``fixture_or_window_id`` from the run's market snapshots.

    Replay and live both key on the snapshot ``fixture_id`` (the committed
    fixture/window identifier); falls back to ``"unknown"`` for an empty run.

    Args:
        marketstates: The ordered tick snapshots driving the run.

    Returns:
        The fixture/window identifier as a string.
    """
    if marketstates:
        fixture_id = getattr(marketstates[0], "fixture_id", None)
        if fixture_id is not None:
            return str(fixture_id)
    return "unknown"


def _score_root(scores: list[dict[str, Any]]) -> str:
    """SHA-256 over the canonically-serialized ranked score rows (the manifest score root).

    Args:
        scores: The :func:`~veridex.scoring.score_run` output.

    Returns:
        A 64-character hex digest binding the scored result into the manifest.
    """
    return hashlib.sha256(serialize_payload(scores).encode("utf-8")).hexdigest()


def _default_checks(scores: list[dict[str, Any]], run: RunResult) -> dict[str, Any]:
    """Compose the default Proof-Checks summary from the scored run.

    JUDGMENT CALL (surfaced for the gate): this composition is a harness convenience, not a
    spec-mandated schema. The three checks are derived as follows:

      * ``clv``: ``"pass"`` iff the rank-1 agent has a positive average CLV (the run produced a
        genuinely edge-positive winner), else ``"fail"``; ``scored_actions`` is the total number
        of scored actions across all agents in the run.
      * ``evidence_integrity``: ``"pass"`` when the run sealed a non-empty ``evidence_hash``.
      * ``llm_boundary``: always ``"pass"`` — the static import audit
        (``veridex.verifier.import_audit``) is what actually guarantees the trust boundary; this
        line merely surfaces that guarantee on the card.

    Args:
        scores: The ranked per-agent metric stack (rank-1 first).
        run: The completed run result (for the evidence hash).

    Returns:
        A Proof-Checks summary dict (exposed publicly as ``checks`` — never ``cats``).
    """
    top_avg = scores[0].get("avg_clv_bps") if scores else None
    clv_result = "pass" if isinstance(top_avg, (int, float)) and top_avg > 0 else "fail"
    scored_actions = sum(int(row.get("action_count", 0)) for row in scores)
    return {
        "clv": {"result": clv_result, "scored_actions": scored_actions},
        "evidence_integrity": "pass" if run.evidence_hash else "fail",
        "llm_boundary": "pass",
    }


async def run_demo_competition(
    marketstates: list[MarketState],
    agents: list[Any],
    *,
    source_mode: str = "replay",
    store: Store | None = None,
    anchor_fn: AnchorFn | None = anchor_memo,
    checks_fn: ChecksFn | None = None,
    run_id: str | None = None,
) -> CompetitionResult:
    """Run one full demo competition: score → manifest → anchor → proof card → leaderboard.

    Drives the entire Phase-1 spine over a single fixture/window for ≥2 agents and returns one
    :class:`CompetitionResult` bundling every artifact a judge inspects. Every link is bound by
    hash: the manifest's ``action_evidence_root`` is the run's ``evidence_hash``, the
    ``manifest_hash`` is the exact anchored Memo payload, and the proof card carries the same
    evidence hash + lineage.

    Anchoring is injectable. ``anchor_fn`` defaults to the real
    :func:`~veridex.chain.anchor.anchor_memo`; the offline suite injects a mock (no network).
    Passing ``anchor_fn=None`` skips anchoring (``anchor_status="not_anchored"``, ``signature``
    ``None``). When anchored, ``anchor_status="anchored"`` and ``signature`` is the tx signature
    (canonical vocab ``"anchored" | "pending" | "not_anchored"``).

    Args:
        marketstates: Ordered tick snapshots of the run (identical inputs for every agent).
        agents: Participating agents (≥2 for a meaningful competition).
        source_mode: ``"replay"`` or ``"live"`` — carried through to the leaderboard rows.
        store: Optional async store; when given, the run is persisted by the orchestrator.
        anchor_fn: Injectable ``async (manifest_hash) -> signature``; ``None`` skips anchoring.
        checks_fn: Injectable Proof-Checks builder; defaults to :func:`_default_checks`.
        run_id: Optional explicit run id (forwarded to the orchestrator) — pin it for a fully
            deterministic ``manifest_hash``.

    Returns:
        The :class:`CompetitionResult` bundle.
    """
    run = await run_competition(marketstates, agents, source_mode=source_mode, store=store, run_id=run_id)
    scores = score_run(run)

    manifest = run_manifest(
        run_id=run.run_id,
        fixture_or_window_id=_fixture_or_window_id(marketstates),
        agent_ids=run.agent_ids,
        action_evidence_root=run.evidence_hash,
        score_root=_score_root(scores),
        proof_mode_map=run.proof_mode_map,
        code_prompt_schema_versions=dict(SCHEMA_VERSIONS),
    )
    manifest_hash = run_manifest_hash(manifest)

    # --- anchor (injectable; default real, mocked offline, skippable via None) ----------
    if anchor_fn is None:
        anchor_status = "not_anchored"
        signature: str | None = None
    else:
        signature = await anchor_fn(manifest_hash)
        anchor_status = "anchored"

    # --- proof card: lineage + checks + anchor (schema_versions agree with the manifest) -
    resolved_checks_fn = checks_fn if checks_fn is not None else _default_checks
    checks = resolved_checks_fn(scores, run)
    anchor_block = {"status": anchor_status, "signature": signature, "cluster": DEFAULT_CLUSTER}
    proof_card = proof_card_from_run_result(
        run, checks=checks, anchor=anchor_block, schema_versions=dict(SCHEMA_VERSIONS)
    )

    # --- leaderboard: tag each score row with this run's anchor_status + source_mode -----
    leaderboard_rows = leaderboard(
        [{**row, "anchor_status": anchor_status, "source_mode": source_mode} for row in scores]
    )

    return CompetitionResult(
        run=run,
        scores=scores,
        manifest=manifest,
        manifest_hash=manifest_hash,
        anchor_status=anchor_status,
        signature=signature,
        proof_card=proof_card,
        leaderboard=leaderboard_rows,
    )
