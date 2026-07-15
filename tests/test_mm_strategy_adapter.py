"""E5-T4 — pending/ambiguous freeze + startup truth (the R4-B→R4-A adapter tier).

OFFLINE ONLY (REQ-093/094/097, AC-017/021, RED-15/16). The R4-A execution facade is an
INJECTABLE seam; every test here drives a RECORDING FAKE facade that counts calls and returns a
scripted :class:`MMExecutionToolResult`. No real facade call, no network, no wallet/signer/order/
submit/cancel, no Mode-B arm. The "exactly one facade call" assertions count calls on the FAKE.

The freeze safety boundary (REQ-093): the moment a fresh-write leg returns an UNCERTAIN outcome —
a plain ``SUBMITTED`` (possibly-unresolved ACK), a pending-reconciliation freeze
(``attempt_pending_reconciliation``), or a withheld ``mode_b_not_armed`` — every remaining
fresh-write leg is FROZEN and NO further fresh-write facade call is issued. ``SUBMITTED`` is never
assumed filled or withdrawn; the book is not treated flat until a reconciled projection confirms.
Resumption comes ONLY from a new observation/projection decision, never a within-plan retry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from veridex.dust_execution.facade import MMExecutionToolResult
from veridex.mm_strategy.contracts import InventoryProjection, NeutralIntent
from veridex.mm_strategy.core import projection_startup_gate
from veridex.mm_strategy.execution_adapter import (
    execute_plan,
    freezes_fresh_writes,
    is_possibly_unresolved,
)

# --- builders ---------------------------------------------------------------------------------


def _fresh_write(client_order_id: str, *, leg_role: str = "bid", price: float = 0.49) -> NeutralIntent:
    """A fresh-write (``place_quote``) leg — the kind subject to the freeze boundary."""
    return NeutralIntent(
        kind="place_quote", leg_role=leg_role, price=price, client_order_id=client_order_id
    )


def _cancel_leg() -> NeutralIntent:
    """A risk-reducing cancel leg (never a fresh write)."""
    return NeutralIntent(kind="cancel_all_orders", leg_role=None, price=None)


def _result(
    *,
    admission: str = "APPROVED",
    execution_status: str = "ABSTAINED",
    reason_codes: tuple[str, ...] = (),
    execution_reason_codes: tuple[str, ...] = (),
) -> MMExecutionToolResult:
    """A typed boundary result with the honest pinned labels; only the disposition fields vary."""
    return MMExecutionToolResult(
        admission=admission,  # type: ignore[arg-type]
        reason_codes=reason_codes,
        execution_status=execution_status,  # type: ignore[arg-type]
        execution_reason_codes=execution_reason_codes,
        lifecycle_receipt_ref="dust-lifecycle:test:deadbeefdeadbeef",
        run_label="DUST_LIVE",
        calibration_label="UNCALIBRATED",
        edge_label="NOT_PROVEN_EDGE",
        evidence_class="EXPERIMENTAL_DUST",
        policy_hash="policy-hash",
    )


@dataclass
class _RecordingFakeFacade:
    """OFFLINE recording fake — counts calls and returns a scripted result per call.

    NEVER a real facade: no network, no wallet/signer, no submit/cancel. The scripted ``results``
    are replayed in order (the last is reused if the plan somehow issues more calls than scripted,
    which would itself be a freeze-boundary violation the count assertion catches).
    """

    results: list[MMExecutionToolResult]
    calls: list[NeutralIntent] = field(default_factory=list)

    def __call__(self, leg: NeutralIntent) -> MMExecutionToolResult:
        result = self.results[min(len(self.calls), len(self.results) - 1)]
        self.calls.append(leg)
        return result

    @property
    def call_count(self) -> int:
        return len(self.calls)


# --- freeze on pending / uncertain ACK -------------------------------------------------------


def test_pending_leg_freezes_fresh_writes() -> None:
    """AC-017/RED-15: a leg returning ``attempt_pending_reconciliation`` freezes the remaining
    fresh-write legs — no further facade call is issued."""
    plan = (_fresh_write("A"), _fresh_write("B"), _fresh_write("C"))
    pending = _result(
        admission="DENIED",
        reason_codes=("attempt_pending_reconciliation",),
        execution_status="NOT_ARMED",
        execution_reason_codes=("mode_b_not_armed",),
    )
    facade = _RecordingFakeFacade(results=[pending])

    result = execute_plan(plan, facade)

    assert facade.call_count == 1  # only leg A was attempted
    assert result.frozen is True
    assert result.outcomes[0].attempted is True
    assert result.outcomes[1].frozen is True  # B never attempted
    assert result.outcomes[2].frozen is True  # C never attempted
    assert result.awaiting_reconciliation is True


def test_submitted_is_possibly_unresolved_not_filled() -> None:
    """REQ-093: a ``SUBMITTED`` leg is possibly-unresolved — NEVER assumed filled or withdrawn."""
    submitted = _result(admission="APPROVED", execution_status="SUBMITTED")
    assert is_possibly_unresolved(submitted) is True

    plan = (_fresh_write("A"),)
    facade = _RecordingFakeFacade(results=[submitted])
    result = execute_plan(plan, facade)

    outcome = result.outcomes[0]
    assert outcome.possibly_unresolved is True
    assert outcome.assumed_filled is False  # never assumed filled
    assert outcome.assumed_withdrawn is False  # never assumed withdrawn
    assert result.book_treated_flat is False  # book not flat until a reconciled projection confirms


def test_first_leg_submitted_freezes_second_fresh_write_exactly_one_facade_call() -> None:
    """Codex's exact object: a TWO-leg placement plan whose leg 1 ACKs ``SUBMITTED`` must issue
    EXACTLY ONE facade call total, freezing the second fresh-write leg until a NEW decision."""
    plan = (_fresh_write("A"), _fresh_write("B"))
    leg1 = MMExecutionToolResult(
        admission="APPROVED",
        execution_status="SUBMITTED",
        execution_reason_codes=(),
        reason_codes=(),
        lifecycle_receipt_ref="dust-lifecycle:test:deadbeefdeadbeef",
        run_label="DUST_LIVE",
        calibration_label="UNCALIBRATED",
        edge_label="NOT_PROVEN_EDGE",
        evidence_class="EXPERIMENTAL_DUST",
        policy_hash="policy-hash",
    )
    facade = _RecordingFakeFacade(results=[leg1])

    result = execute_plan(plan, facade)

    assert facade.call_count == 1  # EXACTLY ONE facade call across the plan
    assert result.outcomes[0].attempted is True
    assert result.outcomes[1].frozen is True  # second fresh write frozen
    assert result.can_resume_within_plan is False  # only a new reconciled-projection decision resumes


def test_cancel_ack_alone_no_replacement() -> None:
    """AC-021/RED-16: a cancel ACK alone does NOT trigger a replacement — no fresh write is placed
    until a reconciled projection permits it."""
    plan = (_cancel_leg(),)
    cancel_ack = _result(admission="APPROVED", execution_status="SUBMITTED")  # cancel reached the wire
    facade = _RecordingFakeFacade(results=[cancel_ack])

    result = execute_plan(plan, facade)

    assert facade.call_count == 1  # only the cancel — no replacement fresh write
    assert result.replacement_triggered is False
    assert all(outcome.leg.kind != "place_quote" for outcome in result.outcomes)
    assert result.book_treated_flat is False  # a cancel ACK is not assumed to have flattened the book


def test_missing_projection_no_quote() -> None:
    """REQ-097: startup truth comes from the INJECTED projection (never a venue query); a missing or
    stale projection is data-degraded → ``NO_QUOTE(projection_stale)``; a fresh one may proceed."""
    missing = projection_startup_gate(None)
    assert missing is not None
    assert missing.kind == "NO_QUOTE"
    assert missing.reason_codes == ("projection_stale",)

    stale = InventoryProjection(net_position=0.0, resting=(), projection_as_of_ts=1, fresh=False)
    stale_decision = projection_startup_gate(stale)
    assert stale_decision is not None
    assert stale_decision.kind == "NO_QUOTE"
    assert stale_decision.reason_codes == ("projection_stale",)

    fresh = InventoryProjection(net_position=0.0, resting=(), projection_as_of_ts=1, fresh=True)
    assert projection_startup_gate(fresh) is None  # present + fresh → startup may proceed


def test_reconcile_path_reached_not_bypassed() -> None:
    """The smm negative fixture: a possibly-unresolved outcome REACHES the reconcile path
    (``awaiting_reconciliation``) instead of early-returning as done/flat."""
    plan = (_fresh_write("A"), _fresh_write("B"))
    submitted = _result(admission="APPROVED", execution_status="SUBMITTED")
    facade = _RecordingFakeFacade(results=[submitted])

    result = execute_plan(plan, facade)

    assert result.awaiting_reconciliation is True  # reconcile path REACHED, not bypassed
    assert result.book_treated_flat is False
    assert result.frozen is True
    assert freezes_fresh_writes(submitted) is True
