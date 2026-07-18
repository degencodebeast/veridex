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

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from veridex.chain.anchor import anchor_memo, run_manifest_hash
from veridex.checks.build import (
    build_check_results,
    build_performance_metrics,
    check_results_to_proof_block,
)
from veridex.leaderboard import leaderboard
from veridex.runtime.arena_comparison import run_arena_comparison
from veridex.runtime.orchestrator import RunResult, run_competition
from veridex.scoring import score_run
from veridex.verifier.proof_card import DEFAULT_SCHEMA_VERSIONS, proof_card_from_run_result
from veridex.verifier.recompute import manifest_from_run, recompute_score_root, root_forest_for_run

if TYPE_CHECKING:
    from veridex.ingest.marketstate import MarketState
    from veridex.runtime.llm_checkpoint import CheckpointPolicy
    from veridex.store import Store
    from veridex.strategies.llm_drift import ModelLauncher

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
        arena_comparison: The OPTIONAL II-9 checkpointed arena-comparison payload (eligible
            checkpoints, per-contestant actions-vs-WAITs, scoreable decisions, fixture count,
            clustered uncertainty, and the identical-opportunity flag). ADDITIVE: defaults to
            ``None`` for every existing (non-arena) run, so current callers are unaffected. It is
            NEVER a bare average CLV (addendum §3) — attach it via :func:`attach_arena_comparison`.
    """

    run: RunResult
    scores: list[dict[str, Any]]
    manifest: dict[str, Any]
    manifest_hash: str
    anchor_status: str
    signature: str | None
    proof_card: dict[str, Any]
    leaderboard: list[dict[str, Any]]
    arena_comparison: dict[str, Any] | None = None


@dataclass(frozen=True)
class ArenaSpec:
    """Opt-in II-9 arena-comparison config, driven over the SAME tape as the scored competition.

    When passed to :func:`run_demo_competition`, the checkpointed det-Drift vs LLM-Drift comparison is
    driven over the run's ``marketstates`` and its HONEST report (eligible checkpoints, per-contestant
    actions-vs-WAITs, AUTHORITATIVE scoreable decisions, fixture count, clustered uncertainty, and the
    identical-opportunity flag) is attached to the returned :class:`CompetitionResult` via
    :func:`attach_arena_comparison` — so a real det-Drift + LLM-Drift competition ACTUALLY produces and
    exposes the fairness/accounting payload reaching the API/UI (NEVER a bare average CLV, addendum §3).

    Attributes:
        model: The injectable LLM launcher seam for LLM-Drift (production Agno launcher; a fake offline).
        det_policy: The pinned checkpoint policy for det-Drift.
        llm_policy: The pinned checkpoint policy for LLM-Drift; defaults to ``det_policy`` (the fair,
            identical-grid case).
        det_kwargs: det-Drift gate thresholds (mirrors ``CumulativeDriftStrategy`` kwargs).
    """

    model: ModelLauncher
    det_policy: CheckpointPolicy
    llm_policy: CheckpointPolicy | None = None
    det_kwargs: dict[str, Any] = field(default_factory=dict)


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
    """Back-compat alias for :func:`veridex.verifier.recompute.recompute_score_root`.

    The score-root formula now lives in the verifier trust path (single source of truth, Task D2);
    this thin re-export keeps existing importers (the API router) working without forking the
    formula.

    Args:
        scores: The :func:`~veridex.scoring.score_run` output.

    Returns:
        A 64-character hex digest binding the scored result into the manifest.
    """
    return recompute_score_root(scores)


def read_path_check_block(scores: list[dict[str, Any]], run: RunResult) -> dict[str, Any]:
    """The read-path (manifest-unavailable) proof block: a 2-arg convenience over the ONE builder.

    Delegates to :func:`~veridex.checks.build.build_check_results` (WD-5b) — the typed 7-member
    Proof-Check taxonomy. There is no separate/divergent dict-based implementation here; this is
    purely a narrower call into the same builder for callers (the API read endpoints) that don't
    have a manifest/anchor/events on hand. CLV is NOT a check here (SEC-001); it lives in the
    separate Performance-Metrics block (:func:`~veridex.checks.build.build_performance_metrics`).

    Manifest/anchor/events are unavailable in this convenience path, so MANIFEST_BOUND/
    POLICY_OBEYED/RECEIPT_SEPARATION are ``not_applicable`` and ANCHOR follows the run's
    ``source_mode`` (SEC-002/SEC-008) — the ONLY difference from the full path in
    :func:`run_demo_competition`, which passes the manifest/anchor/events for the richer verdict.

    Args:
        scores: The ranked per-agent metric stack (rank-1 first) — the PERSISTED/VISIBLE table
            also rendered in the Performance-Metrics block (so METRICS_RECOMPUTED is non-tautological).
        run: The completed run result (evidence hash + run_events + score rows).

    Returns:
        A Proof-Checks block keyed by ``CheckId`` value (exposed publicly as ``checks`` — never a legacy internal name).
    """
    results = build_check_results(scores=scores, run=run, source_mode=run.source_mode)
    return check_results_to_proof_block(results)


async def run_demo_competition(
    marketstates: list[MarketState],
    agents: list[Any],
    *,
    source_mode: str = "replay",
    store: Store | None = None,
    anchor_fn: AnchorFn | None = anchor_memo,
    checks_fn: ChecksFn | None = None,
    run_id: str | None = None,
    arena: ArenaSpec | None = None,
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
        checks_fn: Injectable Proof-Checks builder (tests); when ``None`` the full typed taxonomy
            is built via :func:`~veridex.checks.build.build_check_results` (manifest+anchor bound).
        run_id: Optional explicit run id (forwarded to the orchestrator) — pin it for a fully
            deterministic ``manifest_hash``.

    Returns:
        The :class:`CompetitionResult` bundle.
    """
    run = await run_competition(marketstates, agents, source_mode=source_mode, store=store, run_id=run_id)
    scores = score_run(run)

    # Build the anchored manifest through the verifier's shared helpers (Task D2 — single source of
    # truth) so that ``verify_run(run).manifest`` reconstructs THIS manifest byte-for-byte.
    score_root = recompute_score_root(scores)
    manifest = manifest_from_run(
        run,
        fixture_or_window_id=_fixture_or_window_id(marketstates),
        score_root=score_root,
        schema_versions=dict(SCHEMA_VERSIONS),
    )
    manifest["root_forest"] = root_forest_for_run(run, scores)
    manifest_hash = run_manifest_hash(manifest)

    # --- anchor (injectable; default real, mocked offline, skippable via None) ----------
    if anchor_fn is None:
        anchor_status = "not_anchored"
        signature: str | None = None
    else:
        signature = await anchor_fn(manifest_hash)
        anchor_status = "anchored"

    # --- proof card: 7 typed checks (manifest+anchor bound) + separate metrics block ----
    anchor_block = {"status": anchor_status, "signature": signature, "cluster": DEFAULT_CLUSTER}
    if checks_fn is not None:
        checks = checks_fn(scores, run)  # injected builder (tests) — kept for back-compat
    else:
        checks = check_results_to_proof_block(
            build_check_results(
                scores=scores,
                run=run,
                manifest=manifest,
                manifest_hash=manifest_hash,
                anchor=anchor_block,
                source_mode=source_mode,
            )
        )
    metrics = build_performance_metrics(scores)  # Performance Metrics (SEC-001): CLV lives here
    proof_card = proof_card_from_run_result(
        run, checks=checks, anchor=anchor_block, schema_versions=dict(SCHEMA_VERSIONS), metrics=metrics
    )

    # --- leaderboard: tag each score row with this run's anchor_status + source_mode -----
    leaderboard_rows = leaderboard(
        [{**row, "anchor_status": anchor_status, "source_mode": source_mode} for row in scores]
    )

    result = CompetitionResult(
        run=run,
        scores=scores,
        manifest=manifest,
        manifest_hash=manifest_hash,
        anchor_status=anchor_status,
        signature=signature,
        proof_card=proof_card,
        leaderboard=leaderboard_rows,
    )

    # II-9 — when this is an arena competition, ACTUALLY run the checkpointed det-Drift vs LLM-Drift
    # comparison over the SAME tape and attach its honest report so the fairness/accounting payload
    # (and the leaderboard's identical-opportunity context) reaches the API/UI — never a harness leaf.
    if arena is not None:
        arena_run = await run_arena_comparison(
            marketstates,
            model=arena.model,
            det_policy=arena.det_policy,
            llm_policy=arena.llm_policy,
            **dict(arena.det_kwargs),
        )
        result = attach_arena_comparison(result, arena_run.report().to_payload())

    return result


def attach_arena_comparison(
    result: CompetitionResult, comparison: dict[str, Any]
) -> CompetitionResult:
    """Return a COPY of ``result`` carrying the II-9 arena-comparison payload (additive, non-mutating).

    The payload is the honest :meth:`~veridex.runtime.arena_comparison.ArenaComparisonReport.to_payload`
    dict — eligible checkpoints, per-contestant actions-vs-WAITs, scoreable decisions, fixture count,
    clustered uncertainty, and the identical-opportunity flag; NEVER a bare average CLV (addendum §3).
    Each leaderboard row is additively tagged with the run-level ``identical_opportunities`` flag and
    ``arena_scoreable_decisions`` so the ranked view carries the honest context without dropping or
    reshaping any existing field. The input ``result`` is left untouched (``CompetitionResult`` is frozen).

    Args:
        result: The completed competition result to enrich.
        comparison: The arena-comparison payload to attach (from ``ArenaComparisonReport.to_payload``).

    Returns:
        A new :class:`CompetitionResult` with ``arena_comparison`` set and the tagged leaderboard rows.
    """
    from dataclasses import replace

    identical = comparison.get("identical_opportunities")
    scoreable = comparison.get("scoreable_decisions")
    tagged_rows = [
        {**row, "identical_opportunities": identical, "arena_scoreable_decisions": scoreable}
        for row in result.leaderboard
    ]
    return replace(result, arena_comparison=dict(comparison), leaderboard=tagged_rows)
