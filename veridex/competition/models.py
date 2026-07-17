"""Competition domain models — Phase 2A Task 1.

Pure value objects: enums, Pydantic models, and the Competition status lifecycle method.
No I/O, no async, no LLM imports. The trust-path import audit (veridex.verifier.import_audit)
enforces the LLM-free boundary statically.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from veridex.policy.envelope import PolicyEnvelope


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
    """Lifecycle state of a competition; transitions are strictly monotonic.

    Note:
        ``OPEN`` is a reserved registration-window state. It is NOT used in the
        Phase-2A / 2B happy path (draft → running → finalized); a future control-plane
        endpoint (Task 7+) will use it for agent self-registration flows.
    """

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
        operator_id: Optional identifier for the operator / control-plane account that
            owns this competition; used by the Task-7 control-plane auth layer.
        policy_envelope: Optional operator-set execution guardrail envelope (Phase-2B). When
            ``None``, the service builds a conservative deny-by-default envelope for non-paper
            runs. The control-plane kill-switch endpoint mutates ``kill_switch`` here.
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
    operator_id: str | None = None
    policy_envelope: PolicyEnvelope | None = None


class AgentEntry(BaseModel):
    """A single agent registered in a competition roster.

    Attributes:
        agent_id: Unique agent identifier.
        owner: Owner / team identifier.
        strategy: Strategy label (e.g. ``"kelly_clv_v2"``).
        model: Optional LLM model slug used by the agent.
        proof_mode: Raw proof-mode string; normalisation is deferred to Task 3.
        config_hash: Optional content-hash of the agent's config snapshot. When ``instance_id`` is
            set, this pins the referenced Studio-deployed instance's ``config_hash`` — the roster's
            identity commitment that the arena verifies against the live instance at start (I-7).
        execution_eligibility: Whether the agent is cleared to execute.
        instance_id: Optional reference to a Studio-deployed :class:`~veridex.deploy.instance.AgentInstance`
            (I-7 roster->instance binding). When set, the arena runs the ACTUAL deployed contestant —
            it builds the agent from that instance's pinned ``effective_config`` and refuses (fail-closed)
            if the live instance's ``config_hash`` has drifted from the pinned ``config_hash`` above.
            Optional + legacy-compatible: a roster entry without a deployed instance leaves it ``None``
            and is built by its declared ``strategy`` (mirrors I-2's optional-field idiom).
    """

    agent_id: str
    owner: str
    strategy: str
    model: str | None
    proof_mode: str  # normalisation deferred to Task 3; kept as plain str here
    config_hash: str | None = None
    execution_eligibility: bool = False
    instance_id: str | None = None


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
        owner_id: The SERVER-DERIVED owner identity (the authenticated Privy principal's DID) set
            when the competition is created (I-7b). Optional + legacy-compatible: a pre-change
            competition persisted without an ``owner_id`` still loads with ``None`` and is treated
            as UNOWNED (never silently granted to any caller — fail-closed, mirrors I-2's
            ``AgentInstance.operator_id``).
    """

    competition_id: str
    config: CompetitionConfig
    status: CompetitionStatus
    entries: list[AgentEntry]
    run_id: str | None
    owner_id: str | None = None

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


# Maps all known wire/API proof-mode labels to the two canonical Phase-2A values.
# Phase-1 emits "reproducible" and "LLM/evidence-verified"; both are mapped here.
_PROOF_MODE_MAP: dict[str, Literal["reproducible", "verified"]] = {
    "reproducible": "reproducible",
    "verified": "verified",
    "LLM/evidence-verified": "verified",
}


def normalize_proof_mode(value: str) -> Literal["reproducible", "verified"]:
    """Normalise a wire proof-mode string to one of the two canonical values.

    Accepts Phase-1 labels (``"reproducible"``, ``"LLM/evidence-verified"``) and
    the canonical Phase-2A label (``"verified"``), mapping all to exactly
    ``"reproducible"`` or ``"verified"`` per REQ-219 / AC-216.

    Args:
        value: Raw proof-mode string from the wire or API boundary.

    Returns:
        The canonical proof mode: ``"reproducible"`` or ``"verified"``.

    Raises:
        ValueError: If ``value`` is not a recognised proof-mode label.
    """
    try:
        return _PROOF_MODE_MAP[value]
    except KeyError:
        raise ValueError(f"unknown proof_mode: {value!r}") from None
