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

import inspect
from dataclasses import dataclass, field
from typing import get_args

from veridex.dust_execution.facade import IntentKind, MMExecutionToolResult
from veridex.mm_strategy.contracts import (
    InventoryProjection,
    NeutralIntent,
    NeutralIntentKind,
    StrategyDecision,
)
from veridex.mm_strategy.core import projection_startup_gate
from veridex.mm_strategy.execution_adapter import (
    NEUTRAL_TO_R4A,
    R4ARequestConfig,
    build_r4a_request,
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


# --- E5-T5: neutral→R4-A mapping + singular request + no size/take -----------------------------


def _pinned_config() -> R4ARequestConfig:
    """The ONE pinned request config — every hash/id/mode/sizing input is pinned session config,
    NEVER agent-supplied (REQ-058). ``wallet_equity``/``fixed_fraction`` are the pinned sizing
    inputs the adapter threads to R4-A's proposer; the adapter itself never sizes."""
    return R4ARequestConfig(
        strategy_id="mm-dust",
        strategy_config_hash="cfg-hash",
        policy_hash="policy-hash",
        session_id="sess-1",
        manifest_hash="manifest-hash",
        mode="dry_run",
        wallet_equity_at_decision=1000.0,
        fixed_fraction=0.001,
    )


def test_mapping_total_exact_image_take_excluded() -> None:
    """§6.3(5): ``NEUTRAL_TO_R4A`` is TOTAL over the neutral domain, its image is EXACTLY the
    non-aggressive 4-set, and the aggressive ``take`` kind is UNREACHABLE (never in the image)."""
    neutral_members = set(get_args(NeutralIntentKind))
    intent_members = set(get_args(IntentKind))

    # TOTAL: every NeutralIntentKind member is a key (no neutral kind is left unmapped).
    assert set(NEUTRAL_TO_R4A.keys()) == neutral_members

    # EXACT image: the closed non-aggressive 4-set — no more, no less.
    assert set(NEUTRAL_TO_R4A.values()) == {"make_quote", "cancel_replace", "cancel_all", "no_quote"}

    # ``take`` (the aggressive kind) is NEVER in the image — it is unrepresentable by design.
    assert "take" not in set(NEUTRAL_TO_R4A.values())
    assert "take" in intent_members  # (sanity: ``take`` IS a valid R4-A kind — just never emitted)

    # Every image value is a real R4-A ``IntentKind`` (no minted synonym).
    assert set(NEUTRAL_TO_R4A.values()) <= intent_members

    # The exact pinned pairing.
    assert dict(NEUTRAL_TO_R4A) == {
        "place_quote": "make_quote",
        "replace_quote": "cancel_replace",
        "cancel_all_orders": "cancel_all",
        "abstain": "no_quote",
    }


def test_adapter_pins_evidence_class_not_caller_param() -> None:
    """The adapter PINS ``evidence_class="EXPERIMENTAL_DUST"`` — it is a constant in the adapter,
    never a caller/agent argument (not on the build function, not on the pinned config)."""
    config = _pinned_config()
    request = build_r4a_request(_fresh_write("A"), config)

    assert request.evidence_class == "EXPERIMENTAL_DUST"

    # NOT a caller/agent parameter of the adapter's request builder.
    builder_params = inspect.signature(build_r4a_request).parameters
    assert "evidence_class" not in builder_params

    # NOT a field of the pinned config either — an agent cannot smuggle it in via config.
    assert "evidence_class" not in set(R4ARequestConfig.__dataclass_fields__)


def test_adapter_sets_no_size() -> None:
    """RED-22 / REQ-058: the adapter sets NO agent size on the built request — sizing is deferred
    to R4-A's ``resolve_dust_size`` (the sole wire-size authority)."""
    config = _pinned_config()

    # A fresh-write (make_quote) leg is the sizeable case — the adapter still sets no size.
    make_request = build_r4a_request(_fresh_write("A"), config)
    assert make_request.intent_params.size is None

    # Holds for every neutral kind: no built request ever carries an adapter-set size.
    for leg in (
        _fresh_write("A"),
        NeutralIntent(kind="replace_quote", leg_role="bid", price=0.5, client_order_id="B"),
        _cancel_leg(),
        NeutralIntent(kind="abstain", leg_role=None, price=None),
    ):
        assert build_r4a_request(leg, config).intent_params.size is None


def test_reason_confidence_cannot_move_decision() -> None:
    """AC-024 / RED-21: untrusted FV metadata (reason / confidence / proof status) has ZERO effect
    on the mapping or the built request — it is never an input to, nor forwarded by, the adapter."""
    config = _pinned_config()
    leg = _fresh_write("A")

    # The adapter never forwards untrusted agent metadata: the request carries no reason/confidence.
    request = build_r4a_request(leg, config)
    assert request.reason is None
    assert request.confidence is None

    # There is no untrusted-metadata CHANNEL into the builder — only the trusted leg + pinned config.
    builder_params = set(inspect.signature(build_r4a_request).parameters)
    assert builder_params == {"leg", "config"}
    assert not (builder_params & {"reason", "confidence", "proof_status", "fv_message_id"})

    # Two decisions that DIFFER only in untrusted FV metadata carry the SAME leg → identical request.
    hot = StrategyDecision(
        kind="QUOTE_TWO_SIDED",
        intent_plan=(leg,),
        fv_message_id="msg-hot",
        fv_proof_status="proven",
    )
    cold = StrategyDecision(
        kind="QUOTE_TWO_SIDED",
        intent_plan=(leg,),
        fv_message_id="msg-cold",
        fv_proof_status="absent",
    )
    assert build_r4a_request(hot.intent_plan[0], config) == build_r4a_request(cold.intent_plan[0], config)

    # The intent kind is a pure function of the TRUSTED leg.kind — nothing else moves it.
    assert request.intent_kind == NEUTRAL_TO_R4A[leg.kind]
