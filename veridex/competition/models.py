"""Competition domain models — Phase 2A Task 1.

Pure value objects: enums, Pydantic models, and the Competition status lifecycle method.
No I/O, no async, no LLM imports. The trust-path import audit (veridex.verifier.import_audit)
enforces the LLM-free boundary statically.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class CompetitionType(str, Enum):
    """Categorises the competition format."""

    LIVE_ARENA = "live_arena"
    REPLAY_ARENA = "replay_arena"
    HEAD_TO_HEAD = "head_to_head"
    PRIZE_VAULT_CHALLENGE = "prize_vault_challenge"


class ExecutionMode(str, Enum):
    """Controls whether agent decisions touch real capital."""

    PAPER = "paper"
    DRY_RUN = "dry_run"
    LIVE_GUARDED = "live_guarded"


class CompetitionStatus(str, Enum):
    """Lifecycle state of a competition; transitions are strictly monotonic."""

    DRAFT = "draft"
    OPEN = "open"
    RUNNING = "running"
    FINALIZED = "finalized"


# Defined before CompetitionConfig so annotations resolve without model_rebuild().


class RewardPolicy(BaseModel):
    """Inert placeholder for the Phase 2D reward-policy layer.

    Only `kind` is tracked here; full reward logic is out-of-scope for Phase 2A.
    """

    kind: str = "badge_only"


class PrizeVaultRef(BaseModel):
    """Inert placeholder for the Phase 2D prize-vault integration."""

    vault_id: str | None = None


class CompetitionConfig(BaseModel):
    """Immutable configuration snapshot for a competition.

    Attributes:
        competition_type: Format variant (live arena, replay, etc.).
        source_mode: Whether market data comes from a live feed or a stored replay.
        execution_mode: Capital-exposure guard; defaults to paper trading.
        market_scope: Free-form market / event selector (e.g. ``"WC:FRA-BRA"``).
        scoring_window: Optional ISO-8601 duration bounding the scoring period.
        roster_size: Minimum 2 agents required to form a valid competition.
        division: Optional bracket / tier label for multi-division events.
        reward_policy: Phase 2D reward configuration; ``None`` means badge-only.
        prize_vault_ref: Phase 2D vault reference; ``None`` means no on-chain prize.
    """

    competition_type: CompetitionType
    source_mode: Literal["replay", "live"]
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    market_scope: str
    scoring_window: str | None = None
    roster_size: int = Field(ge=2)
    division: str | None = None
    reward_policy: RewardPolicy | None = None
    prize_vault_ref: PrizeVaultRef | None = None


class AgentEntry(BaseModel):
    """A single agent registered in a competition roster.

    Attributes:
        agent_id: Unique agent identifier.
        owner: Owner / team identifier.
        strategy: Strategy label (e.g. ``"kelly_clv_v2"``).
        model: Optional LLM model slug used by the agent.
        proof_mode: Raw proof-mode string; normalisation is deferred to Task 3.
        config_hash: Optional content-hash of the agent's config snapshot.
        execution_eligibility: Whether the agent is cleared to execute.
    """

    agent_id: str
    owner: str
    strategy: str
    model: str | None
    proof_mode: str  # normalisation deferred to Task 3; kept as plain str here
    config_hash: str | None = None
    execution_eligibility: bool = False


# Allowed forward transitions: each status maps to the one valid next status.
_NEXT_STATUS: dict[CompetitionStatus, set[CompetitionStatus]] = {
    CompetitionStatus.DRAFT: {CompetitionStatus.OPEN},
    CompetitionStatus.OPEN: {CompetitionStatus.RUNNING},
    CompetitionStatus.RUNNING: {CompetitionStatus.FINALIZED},
    CompetitionStatus.FINALIZED: set(),
}


class Competition(BaseModel):
    """Top-level aggregate for a competition run.

    Attributes:
        competition_id: Stable unique identifier.
        config: Immutable configuration snapshot.
        status: Current lifecycle state.
        entries: Registered agent roster.
        run_id: Optional correlation ID for the active simulation / live run.
    """

    competition_id: str
    config: CompetitionConfig
    status: CompetitionStatus
    entries: list[AgentEntry]
    run_id: str | None

    def advance_status(self, new: CompetitionStatus) -> None:
        """Mutate status to `new`, enforcing monotonic forward-only transitions.

        The allowed transition table is:
            draft → open → running → finalized

        Args:
            new: The target status to transition into.

        Raises:
            ValueError: If `new` is not a valid next step from the current status,
                including same-status, backward, or multi-step skip transitions.
        """
        allowed = _NEXT_STATUS[self.status]
        if new not in allowed:
            raise ValueError(f"illegal competition status transition: {self.status.value} -> {new.value}")
        self.status = new
