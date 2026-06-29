"""Pydantic response models for the Veridex FastAPI surface (B11b — REQ-115 / AC-115; Task 6 — P2A-6).

All models are Google-docstring annotated and carry full type hints so the OpenAPI schema
is accurate and mypy is satisfied without stubs.

Note: ``ProofCardResponse`` is intentionally left as a plain ``dict[str, Any]`` pass-through
(the proof card's nested structure is already validated by ``veridex.verifier.proof_card``);
these models cover the typed response envelopes that the API owns.

Phase-2A adds six competition endpoint response models below the Phase-1 trio.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class LeaderboardRow(BaseModel):
    """One ranked agent row from the cross-run leaderboard.

    Mirrors the output of ``veridex.leaderboard.leaderboard()`` for one agent.

    Attributes:
        rank: 1-based rank position (best agent is rank 1).
        agent_id: Stable agent identifier.
        runs: Number of runs this agent participated in.
        avg_clv_bps: Pooled average CLV in basis-points; ``None`` when action_count is 0.
        total_clv_bps: Sum of CLV across all scored actions.
        sim_pnl: Closing-referenced flat-stake PnL proxy (equals ``total_clv_bps`` in Phase 1).
        brier: Mean Brier score when confidence was emitted; ``None`` otherwise.
        max_drawdown: Worst peak-to-trough drop across runs (``<= 0.0``).
        action_count: Total scored actions across all runs.
        valid_pct: Law-acceptance percentage (valid decisions / total decisions × 100).
        proof_mode: Summarised proof mode across runs.
        eligibility_badge: ``"fully-proven"``, ``"partially-proven"``, or ``"unproven"``.
        anchor_status: ``"all-anchored"``, ``"some-pending"``, or ``"none-anchored"``.
        source_mode: ``"all-replay"``, ``"all-live"``, ``"mixed"``, or ``"unknown"``.
    """

    rank: int
    agent_id: str
    runs: int
    avg_clv_bps: float | None
    total_clv_bps: int
    sim_pnl: int
    brier: float | None
    max_drawdown: float
    action_count: int
    valid_pct: float
    proof_mode: str
    eligibility_badge: str
    anchor_status: str
    source_mode: str


class LeaderboardResponse(BaseModel):
    """Response envelope for ``GET /leaderboard``.

    Attributes:
        rows: Ranked leaderboard rows, best agent first.
    """

    rows: list[LeaderboardRow]


class DemoRunResponse(BaseModel):
    """Response envelope for ``POST /demo/run``.

    Bundles every artifact a judge needs to inspect a single demo competition.

    Attributes:
        run_id: Unique run identifier (hex UUID).
        anchor_status: Canonical anchor vocabulary — ``"anchored"`` or ``"not_anchored"``.
        leaderboard: Ranked leaderboard rows for this run (one per agent).
        proof_card: Full proof-card JSON (lineage + checks + anchor + evidence).
    """

    run_id: str
    anchor_status: str
    leaderboard: list[LeaderboardRow]
    proof_card: dict[str, Any]


# ---------------------------------------------------------------------------
# Phase-2A competition endpoint response models (Task 6 — P2A-6)
# ---------------------------------------------------------------------------


class CompetitionCreateResponse(BaseModel):
    """Response envelope for ``POST /competitions``.

    Attributes:
        competition_id: Stable unique identifier for the created competition.
        status: Lifecycle status (always ``"draft"`` on creation).
    """

    competition_id: str
    status: str


class AgentRegisterResponse(BaseModel):
    """Response envelope for ``POST /competitions/{id}/agents``.

    Attributes:
        agent_id: The agent's unique identifier.
        config_hash: Pinned SHA-256 content-hash of the agent config snapshot (CON-207).
        proof_mode: Canonical proof mode (``"reproducible"`` or ``"verified"``).
    """

    agent_id: str
    config_hash: str | None
    proof_mode: str


class CompetitionStartResponse(BaseModel):
    """Response envelope for ``POST /competitions/{id}/start``.

    Attributes:
        competition_id: Stable unique identifier.
        status: Lifecycle status (``"finalized"`` after a successful synchronous run).
        run_id: Sealed Phase-1 run identifier (set after start).
    """

    competition_id: str
    status: str
    run_id: str | None


class CompetitionLeaderboardRow(BaseModel):
    """One row in the competition-scoped leaderboard, derived from ``SCORE_UPDATE`` events.

    Single source of truth: derived from the persisted canonical event log, not recomputed
    from raw scores.  Ranking is by ``mean_clv_bps`` descending (``None`` treated as ``-inf``),
    then ``agent_id`` ascending as a stable tie-breaker.

    Attributes:
        rank: 1-based rank position (best agent is rank 1).
        agent_id: Agent identifier.
        total_clv_bps: Sum of CLV in basis-points across scored actions.
        mean_clv_bps: Mean CLV in basis-points (``None`` if no scored actions).
        valid_count: Number of law-valid decisions.
        proof_mode: Canonical proof mode for this agent (``None`` if absent from log).
    """

    rank: int
    agent_id: str
    total_clv_bps: int
    mean_clv_bps: float | None
    valid_count: int
    proof_mode: str | None


class CompetitionStateResponse(BaseModel):
    """Response envelope for ``GET /competitions/{id}``.

    Attributes:
        competition_id: Stable unique identifier.
        status: Current lifecycle status (``"draft"``, ``"running"``, ``"finalized"``…).
        config: Immutable configuration snapshot (serialized as plain dict).
        roster: Registered agent entries (serialized as plain dicts).
        leaderboard: Ranked rows derived from ``SCORE_UPDATE`` events in the canonical log.
        latest_seq: Maximum ``seq`` in the persisted event log (``0`` when no events yet).
        anchor_status: Anchor status from the ``PROOF_ANCHOR`` event (``"not_anchored"`` if absent).
        run_id: Sealed Phase-1 run identifier (``None`` until start completes).
    """

    competition_id: str
    status: str
    config: dict[str, Any]
    roster: list[dict[str, Any]]
    leaderboard: list[CompetitionLeaderboardRow]
    latest_seq: int
    anchor_status: str
    run_id: str | None
    proof_card: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None


class KillSwitchResponse(BaseModel):
    """Response envelope for ``POST /competitions/{id}/kill-switch``.

    Attributes:
        competition_id: The competition whose envelope was toggled.
        kill_switch: The new (post-flip) kill-switch state.
        status: Human-readable status (``"kill_switch_on"`` / ``"kill_switch_off"``).
    """

    competition_id: str
    kill_switch: bool
    status: str


class ApprovalResponse(BaseModel):
    """Response envelope for ``POST /executions/{id}/approve``.

    Attributes:
        execution_id: The resolved execution record.
        decision: ``"approved"`` (re-check clean → submitted) or ``"rejected"`` (fail-closed).
        status: The execution record's lifecycle status after resolution.
    """

    execution_id: str
    decision: str
    status: str


class CompetitionSummaryResponse(BaseModel):
    """Summary item for ``GET /competitions`` (list endpoint).

    Attributes:
        competition_id: Stable unique identifier.
        status: Current lifecycle status.
        config: Immutable configuration snapshot (serialized as plain dict).
        run_id: Sealed Phase-1 run identifier (``None`` until start completes).
    """

    competition_id: str
    status: str
    config: dict[str, Any]
    run_id: str | None


# ---------------------------------------------------------------------------
# Phase-2C pinned view-model envelopes (Task 0 — API Surface Contract Freeze)
#
# These are the frozen read/control contracts the frontend (Plans C1/C2/D) binds to.
# The backend returns assembled per-screen view-models, never a raw ``RunResult`` (CON-003).
# SEC-001: the ``checks`` block holds ONLY the 7 CheckId; CLV/performance lives in ``metrics``.
# ---------------------------------------------------------------------------


class ProofArtifactResponse(BaseModel):
    """The proof-card view-model the frontend renders (GET /runs/{run_id}). CLV lives in metrics."""

    verifier_version: str
    run: dict[str, Any]
    lineage: dict[str, Any]
    evidence: dict[str, Any]
    checks: dict[str, Any]
    anchor: dict[str, Any]
    metrics: dict[str, Any] | None = None


class VerifyResponse(BaseModel):
    """WD-1 authoritative recompute (POST /runs/{run_id}/verify). C1's VerifyResult binds to this."""

    run_id: str
    verified: bool
    evidence_hash: str
    recomputed_evidence_hash: str
    manifest_hash: str
    checks: dict[str, Any]
    metrics: dict[str, Any] | None = None
    anchor: dict[str, Any]
    proof_card: dict[str, Any]


class InspectorRecord(BaseModel):
    """Per-action forensic view-model (frontend adapter over GET /runs + events)."""

    run_id: str
    agent_id: str
    tick_seq: int
    market_state: dict[str, Any]
    agent_action: dict[str, Any]
    recompute: dict[str, Any]
    clv_bps: int | str
    untrusted_llm_metadata: dict[str, Any]


class FeedHealthResponse(BaseModel):
    """Feed-health view-model (Markets / cockpit feed-health strip)."""

    source_mode: str
    events_per_min: float | None
    ws_live: bool
    last_tick_ts: int | None
    anchor_status: str


class RuntimeEventsResponse(BaseModel):
    """Agent Ops drawer feed (§4.4 OPS channel). Single-field object wrapper (mirrors
    ``LeaderboardResponse{rows}``) so C2 binds field-name-exact to ``.events``; the ``agent_id``
    is already in the request path, never echoed in the body. ``events`` are RuntimeEvent dicts."""

    events: list[dict[str, Any]]
