"""Execution lifecycle models — Phase 2B Task 2.

Pure value objects: enums, Pydantic models, and the ExecutionRecord lifecycle method.
No I/O, no async, no venue/LLM imports. The trust-path import audit (veridex.verifier.import_audit)
enforces the LLM-free boundary statically.

AWAITING_HUMAN is the explicit resting state for an order that a policy evaluation
marked REQUIRES_HUMAN.  It can only be resolved by the Task-7 human-approval endpoint,
which transitions to either POLICY_APPROVED (approved) or REJECTED (denied).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class ExecutionStatus(str, Enum):
    """Lifecycle state of a single execution order.

    Terminals (no outgoing transitions): REJECTED, CANCELLED, EXPIRED,
    SETTLED, VOIDED, UNRESOLVED.
    """

    PROPOSED = "proposed"
    LAW_APPROVED = "law_approved"
    AWAITING_HUMAN = "awaiting_human"
    POLICY_APPROVED = "policy_approved"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    SETTLED = "settled"
    VOIDED = "voided"
    UNRESOLVED = "unresolved"


class ExecutionTransitionError(ValueError):
    """Raised when an illegal execution status transition is attempted."""


# Allowed forward transitions: each status maps to the set of valid next statuses.
# Terminal states map to an empty set — any transition from them is illegal.
_NEXT_EXEC: dict[ExecutionStatus, set[ExecutionStatus]] = {
    ExecutionStatus.PROPOSED: {ExecutionStatus.LAW_APPROVED, ExecutionStatus.REJECTED},
    ExecutionStatus.LAW_APPROVED: {
        ExecutionStatus.POLICY_APPROVED,
        ExecutionStatus.AWAITING_HUMAN,
        ExecutionStatus.REJECTED,
    },
    ExecutionStatus.AWAITING_HUMAN: {ExecutionStatus.POLICY_APPROVED, ExecutionStatus.REJECTED},
    ExecutionStatus.POLICY_APPROVED: {ExecutionStatus.SUBMITTED, ExecutionStatus.REJECTED},
    ExecutionStatus.SUBMITTED: {ExecutionStatus.ACCEPTED, ExecutionStatus.REJECTED, ExecutionStatus.EXPIRED},
    ExecutionStatus.ACCEPTED: {
        ExecutionStatus.FILLED,
        ExecutionStatus.PARTIAL,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.EXPIRED,
    },
    ExecutionStatus.FILLED: {ExecutionStatus.SETTLED, ExecutionStatus.VOIDED, ExecutionStatus.UNRESOLVED},
    ExecutionStatus.PARTIAL: {ExecutionStatus.SETTLED, ExecutionStatus.VOIDED, ExecutionStatus.UNRESOLVED},
    # Terminals
    ExecutionStatus.REJECTED: set(),
    ExecutionStatus.CANCELLED: set(),
    ExecutionStatus.EXPIRED: set(),
    ExecutionStatus.SETTLED: set(),
    ExecutionStatus.VOIDED: set(),
    ExecutionStatus.UNRESOLVED: set(),
}


class ExecutionReceipt(BaseModel):
    """Normalized, secret-free record of what happened at the venue.

    Carries the observable outcome of an order: sizes, price, and status.
    MUST NOT contain authentication tokens, API keys, or any other credentials.

    Attributes:
        execution_id: Stable identifier shared with the parent ExecutionRecord.
        venue: Venue slug (e.g. ``"sx_bet"``).
        market_ref: Venue-specific market or event identifier.
        side: Which side of the market was taken (e.g. ``"over"``, ``"under"``).
        requested_size: Stake or size originally requested.
        filled_size: Stake or size actually matched by the venue.
        price: Decimal odds or price at which the fill occurred.
        status: Execution status at the time the receipt was captured.
        venue_order_id: Opaque order reference returned by the venue; ``None`` for dry-run.
        mode: Execution mode label (e.g. ``"paper"``, ``"dry_run"``, ``"live_guarded"``).
        submitted_at: ISO-8601 timestamp when the order was submitted; ``None`` if not yet sent.
        settled_at: ISO-8601 timestamp when the order was settled; ``None`` if not yet settled.
    """

    execution_id: str
    venue: str
    market_ref: str
    side: str
    requested_size: float
    filled_size: float
    price: float
    status: ExecutionStatus
    venue_order_id: str | None
    mode: str
    submitted_at: str | None = None
    settled_at: str | None = None


class ExecutionRecord(BaseModel):
    """Mutable aggregate tracking an order from inception to settlement.

    Attributes:
        execution_id: Stable unique identifier for this execution attempt.
        competition_id: Parent competition this execution belongs to.
        run_id: Correlation ID for the active simulation / live run.
        agent_id: The agent that originated this order.
        source_sequence_no: Monotonic sequence number from the upstream event source.
        status: Current lifecycle state; mutated only through ``advance()``.
        policy_hash: Content-hash of the policy snapshot that approved this order.
        receipt: Attached receipt once the venue has responded; ``None`` until then.
    """

    execution_id: str
    competition_id: str
    run_id: str
    agent_id: str
    source_sequence_no: int
    status: ExecutionStatus
    policy_hash: str
    receipt: ExecutionReceipt | None = None

    def advance(self, new: ExecutionStatus) -> None:
        """Mutate status to `new`, enforcing the execution lifecycle transition table.

        The allowed transitions mirror the execution pipeline gates:
            proposed → law_approved → (awaiting_human →) policy_approved
            → submitted → accepted → filled / partial → settled

        Rejection and cancellation are valid from multiple pre-settlement states.
        Terminal states (rejected, cancelled, expired, settled, voided, unresolved)
        have no outgoing transitions.

        Args:
            new: The target status to transition into.

        Raises:
            ExecutionTransitionError: If ``new`` is not a valid next step from the
                current status, including same-status, backward, or multi-step skip
                transitions.
        """
        if new not in _NEXT_EXEC[self.status]:
            raise ExecutionTransitionError(f"illegal execution transition: {self.status.value} -> {new.value}")
        self.status = new
