"""R4-B → R4-A execution adapter — the pending/ambiguous FREEZE safety boundary (E5-T4).

The adapter fires the pure strategy's neutral ``intent_plan`` at R4-A's execution facade IN ORDER,
consuming each leg's typed :class:`~veridex.dust_execution.facade.MMExecutionToolResult` (the
STRATEGY ``admission`` + the SEPARATE closed-vocab EXECUTION ``execution_status`` /
``execution_reason_codes``). Its one job here is the REQ-093 freeze boundary: the moment a leg
returns an UNCERTAIN outcome, every remaining fresh-write leg is FROZEN and no further fresh-write
facade call is issued.

An UNCERTAIN outcome is any of:
  * a plain ``SUBMITTED`` — an uncertain ACK; an order may be live-but-unsettled, so the leg is
    POSSIBLY-UNRESOLVED and NEVER assumed filled or withdrawn (Gate#3 MAJOR-4 / Fable m1);
  * an ``attempt_pending_reconciliation`` in the admission ``reason_codes`` — R4-A's
    ``_freeze_pending_reconciliation`` terminal: a prior possibly-live attempt already exists, so the
    retry is a SAFE pending terminal awaiting the production venue-truth reconcile (the freeze keys on
    the ADMISSION reason, not the ``NOT_ARMED`` disposition — Fable m1);
  * a WITHHELD ``mode_b_not_armed`` / ``operator_interlock_unproven`` execution reason (or a
    ``NOT_ARMED`` status) — Mode B could not arm, so continuing to fire fresh writes is unsafe.

The book is NEVER treated flat until a reconciled projection confirms it (cleanup-then-VERIFY); the
adapter synthesizes NO replacement leg — resumption comes ONLY from a NEW observation/projection
decision that carries reconciled truth, never a within-plan retry.

Trust boundary (SEC-003, audited by E5-T6): the adapter imports only typed R4-A READ surfaces and
the injectable facade SEAM — never a raw ``submit_order`` / ``cancel_order`` / signer / vendored
CLOB write handle. It proposes; it does not sign or write.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from veridex.dust_execution.facade import MMExecutionToolResult
from veridex.mm_strategy.contracts import NeutralIntent

# The neutral intent kinds that place a NEW order on the wire — the legs the freeze protects. A
# ``cancel_all_orders`` is a risk-reducing (never fresh-write) leg; ``abstain`` is no wire action.
_FRESH_WRITE_KINDS: frozenset[str] = frozenset({"place_quote", "replace_quote"})

# The neutral intent kinds that reach the wire at all (the adapter calls the facade for these).
_ACTIONABLE_KINDS: frozenset[str] = frozenset(
    {"place_quote", "replace_quote", "cancel_all_orders"}
)

# The ADMISSION reason code that means a prior possibly-live attempt exists — R4-A's
# ``_freeze_pending_reconciliation`` terminal (facade.py:826). Keyed on the admission reason, not the
# disposition (Fable m1).
_PENDING_ADMISSION_REASONS: frozenset[str] = frozenset({"attempt_pending_reconciliation"})

# The EXECUTION reason codes that mean Mode B could not ARM — execution WITHHELD (facade.py:119-121).
_NOT_ARMED_EXECUTION_REASONS: frozenset[str] = frozenset(
    {"mode_b_not_armed", "operator_interlock_unproven"}
)


class ExecutionFacade(Protocol):
    """The injectable facade seam: propose ONE neutral leg, get back the typed boundary result.

    In production this is R4-A's proposer (wired in E5-T5); in every E5-T4 test it is an OFFLINE
    recording fake. The adapter treats it as opaque — it consumes only the typed result.
    """

    def __call__(self, leg: NeutralIntent) -> MMExecutionToolResult: ...


def _has_pending_reconciliation(result: MMExecutionToolResult) -> bool:
    """True if the admission reason marks a prior possibly-live attempt awaiting reconciliation."""
    return any(code in _PENDING_ADMISSION_REASONS for code in result.reason_codes)


def _is_withheld(result: MMExecutionToolResult) -> bool:
    """True if Mode B could not arm, so execution was WITHHELD (no order reached the wire)."""
    if result.execution_status == "NOT_ARMED":
        return True
    return any(code in _NOT_ARMED_EXECUTION_REASONS for code in result.execution_reason_codes)


def is_possibly_unresolved(result: MMExecutionToolResult) -> bool:
    """True when the wire outcome is UNCERTAIN — an order may be live-but-unsettled (REQ-093).

    A plain ``SUBMITTED`` (uncertain ACK) or a pending-reconciliation freeze is possibly-unresolved:
    the book is NOT known flat and the leg is NEVER assumed filled or withdrawn. A cleanly ABSTAINED /
    DENIED / withheld-but-not-pending leg placed no live order and is not possibly-unresolved.
    """
    return result.execution_status == "SUBMITTED" or _has_pending_reconciliation(result)


def freezes_fresh_writes(result: MMExecutionToolResult) -> bool:
    """True when this leg's outcome must FREEZE every remaining fresh-write leg (REQ-093).

    The conservative safety predicate: any NON-clean first-leg outcome halts fresh writes — a
    possibly-unresolved ACK/pending leg (an order may be live) OR a withheld ``mode_b_not_armed`` leg
    (arming failed, so continuing to fire is unsafe). Resumption is a NEW decision's job, never here.
    """
    return is_possibly_unresolved(result) or _is_withheld(result)


@dataclass(frozen=True)
class LegOutcome:
    """The per-leg record: the neutral leg, its facade result (``None`` when frozen), and whether
    the facade was actually called for it."""

    leg: NeutralIntent
    result: MMExecutionToolResult | None
    attempted: bool

    @property
    def frozen(self) -> bool:
        """True when the leg was FROZEN (never attempted, no facade call)."""
        return not self.attempted

    @property
    def possibly_unresolved(self) -> bool:
        """True when an attempted leg's outcome is possibly-unresolved (see module docstring)."""
        return self.result is not None and is_possibly_unresolved(self.result)

    @property
    def assumed_filled(self) -> bool:
        """ALWAYS False: a ``SUBMITTED`` / uncertain leg is NEVER assumed filled (REQ-093). A fill is
        only ever established by a later reconciled projection, never inferred by the adapter."""
        return False

    @property
    def assumed_withdrawn(self) -> bool:
        """ALWAYS False: the adapter never assumes an order was withdrawn either — cleanup-then-VERIFY
        means the book stays potentially-live until a reconciled projection says otherwise."""
        return False


@dataclass(frozen=True)
class PlanExecutionResult:
    """The whole-plan record: the per-leg outcomes plus the two freeze/reconcile invariants."""

    outcomes: tuple[LegOutcome, ...]
    frozen: bool
    awaiting_reconciliation: bool

    @property
    def book_treated_flat(self) -> bool:
        """ALWAYS False: the adapter NEVER treats the book flat (REQ-094). Only a reconciled
        projection may declare the book flat; lacking it, the adapter defers unconditionally."""
        return False

    @property
    def replacement_triggered(self) -> bool:
        """ALWAYS False: the adapter never synthesizes a replacement leg (AC-021). A cancel ACK alone
        places no fresh write — the replacement, if any, comes from a later reconciled-projection
        decision. This holds structurally: the adapter only ever executes the given plan's legs."""
        return False

    @property
    def can_resume_within_plan(self) -> bool:
        """ALWAYS False: a frozen plan NEVER resumes via a within-plan retry (REQ-093). Resumption
        requires a NEW observation/projection decision carrying reconciled truth."""
        return False


def execute_plan(
    intent_plan: tuple[NeutralIntent, ...],
    facade: ExecutionFacade,
) -> PlanExecutionResult:
    """Fire each actionable leg through the injected facade IN ORDER, freezing on the first uncertain
    outcome (REQ-093/094).

    Walks the plan once. A non-actionable (``abstain``) leg is recorded and skipped. Each actionable
    leg is proposed to the facade EXACTLY once; the moment a leg's outcome
    :func:`freezes_fresh_writes`, every remaining leg is FROZEN (no further facade call). A
    possibly-unresolved outcome sets ``awaiting_reconciliation`` — the reconcile path is REACHED, not
    bypassed, so the book is never early-returned as flat. The adapter adds NO leg of its own.
    """
    outcomes: list[LegOutcome] = []
    frozen = False
    awaiting_reconciliation = False

    for leg in intent_plan:
        if frozen or leg.kind not in _ACTIONABLE_KINDS:
            # Frozen (a prior leg halted the plan) OR a no-wire-action leg: never call the facade.
            outcomes.append(LegOutcome(leg=leg, result=None, attempted=False))
            continue

        result = facade(leg)
        outcomes.append(LegOutcome(leg=leg, result=result, attempted=True))

        if is_possibly_unresolved(result):
            awaiting_reconciliation = True
        if freezes_fresh_writes(result):
            frozen = True

    return PlanExecutionResult(
        outcomes=tuple(outcomes),
        frozen=frozen,
        awaiting_reconciliation=awaiting_reconciliation,
    )
