"""Pydantic response models for the Veridex FastAPI demo surface (B11b — REQ-115 / AC-115).

All models are Google-docstring annotated and carry full type hints so the OpenAPI schema
is accurate and mypy is satisfied without stubs.

Note: ``ProofCardResponse`` is intentionally left as a plain ``dict[str, Any]`` pass-through
(the proof card's nested structure is already validated by ``veridex.verifier.proof_card``);
these models cover the three typed response envelopes that the API owns.
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
