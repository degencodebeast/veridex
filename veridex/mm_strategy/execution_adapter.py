"""R4-B → R4-A execution adapter — the pending/ambiguous FREEZE safety boundary (E5-T4).

The adapter fires the pure strategy's neutral ``intent_plan`` at R4-A's execution facade IN ORDER,
consuming each leg's typed :class:`~veridex.dust_execution.facade.MMExecutionToolResult` (the
STRATEGY ``admission`` + the SEPARATE closed-vocab EXECUTION ``execution_status`` /
``execution_reason_codes``). Its one job here is the REQ-093 freeze boundary: the moment a leg
returns an UNCERTAIN outcome, every remaining leg is frozen (all remaining fresh-write legs under
single-phase plans) and no further facade call is issued.

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

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol

from veridex.dust_execution.contracts import ExecutionMode
from veridex.dust_execution.facade import (
    IntentKind,
    MMExecutionToolRequest,
    MMExecutionToolResult,
    MMIntentParams,
)
from veridex.mm_strategy.contracts import NeutralIntent, NeutralIntentKind

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
    """True when this leg's outcome must FREEZE every remaining leg (REQ-093).

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


# ---------------------------------------------------------------------------
# E5-T5 — neutral→R4-A translation: total mapping + singular request build.
# ---------------------------------------------------------------------------

# The TOTAL neutral→R4-A intent-kind mapping (REQ-091, §6.3(5)). Every ``NeutralIntentKind`` maps,
# and the IMAGE is EXACTLY the closed non-aggressive 4-set ``{make_quote, cancel_replace, cancel_all,
# no_quote}``. R4-A's ``take`` (the AGGRESSIVE cross-the-spread kind) is deliberately UNREACHABLE:
# the pure strategy proposes no aggressive intent, so ``take`` can never be emitted — the honesty
# guarantee §6.3(5) encodes structurally (an aggressive fill is unrepresentable, not merely gated).
# A ``MappingProxyType`` makes the table read-only so no caller can mutate the pinned image.
NEUTRAL_TO_R4A: Mapping[NeutralIntentKind, IntentKind] = MappingProxyType(
    {
        "place_quote": "make_quote",
        "replace_quote": "cancel_replace",
        "cancel_all_orders": "cancel_all",
        "abstain": "no_quote",
    }
)

# The honest evidence class every R4-B dust request is PINNED to — a module constant, NEVER a
# caller/agent parameter (AC-025 consistency; mirrors ``facade._DEFAULT_EVIDENCE_CLASS``). An agent
# cannot relabel a dust run as validated/promoted because it has no channel to supply this at all.
_PINNED_EVIDENCE_CLASS: Literal["EXPERIMENTAL_DUST"] = "EXPERIMENTAL_DUST"


@dataclass(frozen=True)
class R4ARequestConfig:
    """The ONE pinned request config the adapter builds every R4-A request from (REQ-058).

    Every field is PINNED session/manifest config — the operator wires it once; NONE is agent- or
    caller-supplied. The pinned hashes are the admitted pins the strategy declares it operates under
    (declared == admitted: the adapter is the pinned strategy, not an attacker, so it feeds the same
    pins to both sides of :meth:`MMExecutionToolRequest.build`'s fail-closed cross-check).

    ``wallet_equity_at_decision`` / ``fixed_fraction`` are the PINNED mechanical sizing inputs the
    adapter threads to R4-A's ``propose_mm_execution`` — they are NEVER agent-supplied and the adapter
    ITSELF never sizes with them: R4-A's ``resolve_dust_size`` is the sole wire-size authority (REQ-058).
    """

    strategy_id: str
    strategy_config_hash: str
    policy_hash: str
    session_id: str
    manifest_hash: str
    mode: ExecutionMode
    wallet_equity_at_decision: float
    fixed_fraction: float


def _intent_params(leg: NeutralIntent) -> MMIntentParams:
    """Translate a neutral leg's TRUSTED fields into typed R4-A ``MMIntentParams``.

    Carries side (from ``leg_role``), price, and the client-order ids — and NEVER a ``size``: R4-A's
    ``resolve_dust_size`` is the sole sizing authority (REQ-058/RED-22), so ``size`` is left unset
    (``None``). A ``cancel_all_orders`` / ``abstain`` leg carries no side/price/id, so its params are
    empty. Only trusted leg fields flow here; no untrusted agent metadata is read (AC-024).
    """
    side = leg.leg_role if leg.leg_role in ("bid", "ask") else None
    return MMIntentParams(
        side=side,
        price=leg.price,
        client_order_id=leg.client_order_id,
        replaces_client_order_id=leg.replaces_client_order_id,
        # size intentionally UNSET — the adapter never sizes (REQ-058/RED-22).
    )


def build_r4a_request(leg: NeutralIntent, config: R4ARequestConfig) -> MMExecutionToolRequest:
    """Build the SINGULAR typed R4-A request for ONE neutral leg (REQ-091/058, AC-024).

    The intent kind is the TOTAL :data:`NEUTRAL_TO_R4A` mapping of ``leg.kind`` (so ``take`` is
    unreachable), the params carry no adapter-set size, and ``evidence_class`` is PINNED to the
    module constant — never a caller/agent argument. The pinned hashes are declared AND cross-checked
    as admitted (fail-closed via :meth:`MMExecutionToolRequest.build`). Untrusted agent metadata
    (``reason`` / ``confidence`` / FV proof) is NOT a parameter and is NOT forwarded, so it has ZERO
    effect on the mapping or the request (AC-024): the request is a pure function of (trusted leg,
    pinned config). The adapter proposes a typed request; it never sizes, signs, or writes.
    """
    intent_kind = NEUTRAL_TO_R4A[leg.kind]
    return MMExecutionToolRequest.build(
        intent_kind=intent_kind,
        intent_params=_intent_params(leg),
        strategy_id=config.strategy_id,
        strategy_config_hash=config.strategy_config_hash,
        policy_hash=config.policy_hash,
        session_id=config.session_id,
        manifest_hash=config.manifest_hash,
        evidence_class=_PINNED_EVIDENCE_CLASS,  # PINNED — never a caller/agent param (AC-025)
        mode=config.mode,
        admitted_manifest_hash=config.manifest_hash,
        admitted_policy_hash=config.policy_hash,
        admitted_strategy_config_hash=config.strategy_config_hash,
        # reason/confidence deliberately NOT passed: untrusted metadata has zero effect (AC-024).
    )
