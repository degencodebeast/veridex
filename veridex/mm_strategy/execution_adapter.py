"""R4-B → R4-A execution adapter — the pending/ambiguous FREEZE safety boundary (E5-T4).

The adapter walks the pure strategy's single-phase ``intent_plan`` IN ORDER and, for EACH actionable
leg, BUILDS the singular typed :class:`~veridex.dust_execution.facade.MMExecutionToolRequest`
(:func:`build_r4a_request` — the reviewed-observation token derive + decision binding + the
INDEPENDENT admitted-pin cross-check) and hands THAT typed request — never a raw ``NeutralIntent`` —
to R4-A's execution facade, consuming each leg's typed
:class:`~veridex.dust_execution.facade.MMExecutionToolResult` (the STRATEGY ``admission`` + the
SEPARATE closed-vocab EXECUTION ``execution_status`` / ``execution_reason_codes``). Build and execute
are ONE unified path (Gate #4 C-CRITICAL-1): the value the facade consumes is exactly the bound,
pin-cross-checked request whose result controls freezing. Its one job here is the REQ-093 freeze
boundary: the moment a leg returns an UNCERTAIN outcome, every remaining leg is frozen (all remaining
fresh-write legs under single-phase plans) and no further request is built or facade call issued.

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

from veridex.dust_execution.contracts import ExecutionMode, TimeInForce
from veridex.dust_execution.facade import (
    IntentKind,
    MMExecutionToolRequest,
    MMExecutionToolResult,
    MMIntentParams,
)
from veridex.mm_strategy.contracts import (
    NeutralIntent,
    NeutralIntentKind,
    StrategyDecision,
    StrategyObservation,
    reject_mixed_phase_plan,
)

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
    """The injectable facade seam: propose ONE bound typed request, get back the typed result.

    In production this is R4-A's proposer (:func:`~veridex.dust_execution.facade.propose_mm_execution`,
    wired in E5-T5), which consumes an :class:`~veridex.dust_execution.facade.MMExecutionToolRequest`;
    in every E5-T4 test it is an OFFLINE recording fake. The seam consumes the SAME typed request the
    adapter built and pin-cross-checked (Gate #4 C-CRITICAL-1) — never a raw ``NeutralIntent`` — so the
    value whose result controls freezing is exactly the bound, admitted-pin-checked request. The
    adapter treats it as opaque: it consumes only the typed result.
    """

    def __call__(self, request: MMExecutionToolRequest) -> MMExecutionToolResult: ...


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
    def skipped_non_actionable(self) -> bool:
        """True when this leg was a NON-ACTIONABLE (``abstain``) leg that was NEVER going to reach the
        wire (Gate#3 MINOR-1). Such a leg is skipped regardless of any freeze — it is NOT frozen. This
        keys on the SAME :data:`_ACTIONABLE_KINDS` authority ``execute_plan`` uses to decide the skip,
        so the label can never drift from the actual dispatch decision."""
        return not self.attempted and self.leg.kind not in _ACTIONABLE_KINDS

    @property
    def frozen_by_prior_outcome(self) -> bool:
        """True when an ACTIONABLE leg was FROZEN because a PRIOR leg returned an uncertain outcome
        (Gate#3 MINOR-1 / REQ-093). An actionable leg is never left unattempted for any reason other
        than the freeze, so an unattempted actionable leg is precisely a frozen-by-prior-outcome leg."""
        return not self.attempted and self.leg.kind in _ACTIONABLE_KINDS

    @property
    def frozen(self) -> bool:
        """True when the leg was FROZEN by a prior uncertain outcome (Gate#3 MINOR-1). A non-actionable
        ``abstain`` leg that was never going to be attempted is :attr:`skipped_non_actionable`, NOT
        frozen — conflating the two corrupted per-leg audit semantics (a clean ``(cancel, abstain)``
        plan reported ``plan.frozen == False`` yet its abstain leg reported ``frozen == True``)."""
        return self.frozen_by_prior_outcome

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
    decision: StrategyDecision,
    facade: ExecutionFacade,
    *,
    observation: StrategyObservation,
    config: R4ARequestConfig,
    admitted: AdmittedPins,
) -> PlanExecutionResult:
    """Build+fire each actionable leg's BOUND typed request IN ORDER, freezing on the first uncertain
    outcome (REQ-093/094; Gate #4 C-CRITICAL-1).

    UNIFIED build→execute: for EACH actionable leg of the reviewed ``decision.intent_plan`` this
    :func:`build_r4a_request`s the singular typed :class:`MMExecutionToolRequest` — deriving the target
    token from the reviewed ``observation``'s stream identity, BINDING it to the stamped ``decision``,
    and cross-checking the DECLARED ``config`` pins against the INDEPENDENT ``admitted`` authority (so a
    caller-selected declared pin can never self-select the admitted pin) — and hands THAT typed request
    to the facade. The value whose result controls freezing is exactly the bound, pin-cross-checked
    request; the facade never sees a raw ``NeutralIntent``.

    Walks the plan once. A non-actionable (``abstain``) leg is recorded and skipped (no request built,
    no facade call). Each actionable leg is proposed to the facade EXACTLY once; the moment a leg's
    outcome :func:`freezes_fresh_writes`, every remaining leg is FROZEN (no further request built, no
    further facade call). A possibly-unresolved outcome sets ``awaiting_reconciliation`` — the reconcile
    path is REACHED, not bypassed, so the book is never early-returned as flat. The adapter adds NO leg
    of its own; a mismatched declared/admitted pin FAILS CLOSED (``build_r4a_request`` raises) BEFORE any
    facade call, so a rebound intent never reaches the wire.
    """
    # Defense in depth (Gate#3 IMPORTANT-1 / RED-48): a mixed cancel/placement plan is already
    # unconstructable at ``StrategyDecision``; re-assert it here (byte-identity-safe for any valid
    # single-phase decision) so a mixed plan never places a fresh write ahead of a reconciled
    # projection confirming the cancel (REQ-090/094).
    reject_mixed_phase_plan(decision.intent_plan, context="execute_plan")

    outcomes: list[LegOutcome] = []
    frozen = False
    awaiting_reconciliation = False

    for leg in decision.intent_plan:
        if frozen or leg.kind not in _ACTIONABLE_KINDS:
            # Frozen (a prior leg halted the plan) OR a no-wire-action leg: build nothing, call nothing.
            outcomes.append(LegOutcome(leg=leg, result=None, attempted=False))
            continue

        # Build the SINGULAR bound request (reviewed-token derive + decision binding + INDEPENDENT
        # admitted-pin cross-check) and hand THAT typed request to the facade — never a raw intent.
        # A declared/admitted pin mismatch raises here, BEFORE the facade call (fail closed).
        request = build_r4a_request(
            leg, config, observation=observation, decision=decision, admitted=admitted
        )
        result = facade(request)
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

# The physical maker-leg role → R4-A wire SIDE (Gate#3 CRITICAL-1). A neutral ``leg_role`` is
# R4-B-native venue-agnostic vocabulary (``bid`` / ``ask``); R4-A's ``_build_resting_order`` requires
# a wire side in ``{BUY, SELL}`` and returns ``None`` (fail closed → ``intent_params_invalid``) for a
# lowercase role. Forwarding the neutral role LITERALLY made every core quote unwireable — the pure
# core only ever emits ``bid`` / ``ask`` for a placement leg (a reducing leg is still physically a
# bid or ask — ``core._reducing_leg``), so ``reduce`` / ``None`` map to nothing and yield NO side
# (an order-placing leg with no mappable side fails closed at the R4-A boundary, never on the wire).
_ROLE_TO_R4A_SIDE: Mapping[str, Literal["BUY", "SELL"]] = MappingProxyType(
    {"bid": "BUY", "ask": "SELL"}
)

# The honest evidence class every R4-B dust request is PINNED to — a module constant, NEVER a
# caller/agent parameter (AC-025 consistency; mirrors ``facade._DEFAULT_EVIDENCE_CLASS``). An agent
# cannot relabel a dust run as validated/promoted because it has no channel to supply this at all.
_PINNED_EVIDENCE_CLASS: Literal["EXPERIMENTAL_DUST"] = "EXPERIMENTAL_DUST"


@dataclass(frozen=True)
class R4ARequestConfig:
    """The pinned request config the adapter DECLARES every R4-A request under (REQ-058).

    Every field is PINNED session/manifest config — the operator wires it once; NONE is agent- or
    caller-supplied. The hashes here are the pins the strategy DECLARES it operates under; they are
    cross-checked against the SEPARATE, INDEPENDENTLY-SOURCED :class:`AdmittedPins` authority at
    :meth:`MMExecutionToolRequest.build` (Gate #4 C-CRITICAL-1). The two are NEVER the same object, so a
    caller-selected declared pin can NOT also select the admitted pin — self-selection is impossible
    (mirrors R4-A ``runner._authorization_block_reason``'s declared-vs-admitted check, which sources the
    admitted pins from an INDEPENDENT ``manifest`` / ``envelope``, never from the request itself).

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
    # The PINNED maker time-in-force every resting quote is bound to (Gate#3 CRITICAL-1). R4-A's
    # ``_build_resting_order`` requires a TIF in ``{GTC, GTD}`` (a maker rest, never a FAK/FOK taker);
    # ``GTC`` is the operator-pinned session default. Like every field here it is session/manifest
    # config the operator wires once — NEVER agent- or caller-supplied.
    tif: TimeInForce


@dataclass(frozen=True)
class AdmittedPins:
    """The INDEPENDENT admitted-pin authority the declared request pins are cross-checked against.

    Gate #4 C-CRITICAL-1: the manifest / policy / strategy-config hashes the session is ADMITTED under,
    sourced SEPARATELY from the caller's :class:`R4ARequestConfig` DECLARED pins (the operator wires the
    admitted authority once from the reviewed manifest/policy — it is NOT read back from the request
    config). Feeding these as the ``admitted_*`` side of :meth:`MMExecutionToolRequest.build`'s
    fail-closed cross-check restores the R4-A runner's independence property
    (``runner._authorization_block_reason``, runner.py:1158-1170): a caller-selected DECLARED pin
    (e.g. an ``unreviewed-manifest`` under ``mode="live_guarded"``) can never ALSO select the admitted
    pin, so it FAILS CLOSED (no request built, hence no R4-A order and no facade call) instead of being
    self-approved. The adapter's normal operation passes the SAME reviewed hashes on both sides (it is
    the pinned strategy, not an attacker); the value of independence is that a MISMATCH is now
    unrepresentable-as-approved rather than silently accepted.
    """

    manifest_hash: str
    policy_hash: str
    strategy_config_hash: str


def _intent_params(
    leg: NeutralIntent, config: R4ARequestConfig, token_id: str
) -> MMIntentParams:
    """Translate a neutral leg's TRUSTED fields into typed R4-A ``MMIntentParams`` (Gate#3 CRITICAL-1).

    The neutral vocabulary is R4-B-native and must be TRANSLATED to R4-A's wire vocabulary, not
    forwarded literally:

      * ``leg_role`` (``bid`` / ``ask``) → the R4-A ``side`` (``BUY`` / ``SELL``) via
        :data:`_ROLE_TO_R4A_SIDE`. A lowercase role would make ``_build_resting_order`` fail closed,
        so an order-placing leg with no mappable side yields ``side=None`` (fail closed at R4-A).
      * an order-placing (resting) leg carries the reviewed ``token_id`` (the decision's
        ``observation.stream_identity().token_id`` — Gate #2 MAJOR-2 / Gate#3 C-4, so the singular
        request targets EXACTLY the token the decision reviewed) and the config-pinned maker ``tif``.

    Only a leg that actually RESTS an order at R4-A (``make_quote`` / ``cancel_replace`` — those with
    a physical maker side) carries a ``token_id`` / ``tif``; a ``cancel_all_orders`` / ``abstain`` leg
    (no side) rests no order and carries neither. NEVER a ``size``: R4-A's ``resolve_dust_size`` is the
    sole sizing authority (REQ-058/RED-22), so ``size`` is left unset (``None``). Only trusted leg
    fields + pinned config + the reviewed token flow here; no untrusted agent metadata is read (AC-024).
    """
    side = _ROLE_TO_R4A_SIDE.get(leg.leg_role) if leg.leg_role is not None else None
    rests_order = side is not None
    return MMIntentParams(
        token_id=token_id if rests_order else None,
        side=side,
        price=leg.price,
        tif=config.tif if rests_order else None,
        client_order_id=leg.client_order_id,
        replaces_client_order_id=leg.replaces_client_order_id,
        # size intentionally UNSET — the adapter never sizes (REQ-058/RED-22).
    )


def build_r4a_request(
    leg: NeutralIntent,
    config: R4ARequestConfig,
    *,
    observation: StrategyObservation,
    decision: StrategyDecision,
    admitted: AdmittedPins,
) -> MMExecutionToolRequest:
    """Build the SINGULAR typed R4-A request for ONE neutral leg (REQ-091/058, AC-024).

    The target token is DERIVED from the reviewed ``observation.stream_identity().token_id`` and the
    observation is BOUND to the stamped ``decision`` (Gate#3 CRITICAL-1 residual). The caller has NO
    bare-token channel: it cannot name a target token, so it cannot route a price decided from token
    A's book onto a DIFFERENT admitted token B. Two invariants are checked and FAIL CLOSED (raise —
    no request, hence no R4-A resting order and no facade call downstream) before anything is built:

      * ``decision.observation_hash == observation.observation_hash()`` — the ``decision`` must be the
        one actually reviewed FROM this ``observation`` (Gate #2 MAJOR-2 / Gate#3 C-4). A substituted
        observation (a different admitted token) has a different hash and is refused, so the derived
        token can only ever be the one the decision reviewed.
      * ``leg in decision.intent_plan`` — the executed leg must be one the reviewed decision committed
        to; a leg the decision never planned is refused.

    The intent kind is the TOTAL :data:`NEUTRAL_TO_R4A` mapping of ``leg.kind`` (so ``take`` is
    unreachable), the params translate the physical role to an R4-A ``BUY`` / ``SELL`` side and bind
    the DERIVED token + config-pinned maker TIF so the request is WIREABLE at R4-A (Gate#3
    CRITICAL-1), carry no adapter-set size, and ``evidence_class`` is PINNED to the module constant —
    never a caller/agent argument. The ``config`` hashes are DECLARED and cross-checked against the
    SEPARATE, INDEPENDENTLY-SOURCED ``admitted`` :class:`AdmittedPins` authority (Gate #4 C-CRITICAL-1),
    fail-closed via :meth:`MMExecutionToolRequest.build`: a caller-selected declared pin can never
    self-select the admitted pin, so a mismatch RAISES (no request, hence no R4-A order / facade call)
    rather than being self-approved. Untrusted agent metadata (``reason`` /
    ``confidence`` / FV proof) is NOT a parameter and is NOT forwarded, so it has ZERO effect on the
    mapping or the request (AC-024): the request is a pure function of (trusted leg, pinned config,
    reviewed observation, bound decision). The adapter proposes a typed request; it never sizes,
    signs, or writes.
    """
    # Bind the request to the REVIEWED (observation, decision) pair BEFORE building anything, so a
    # caller cannot substitute a different admitted token (Gate#3 CRITICAL-1 residual). Fail closed.
    if decision.observation_hash != observation.observation_hash():
        raise ValueError(
            "build_r4a_request: decision is not bound to the reviewed observation "
            "(decision.observation_hash != observation.observation_hash()) — refusing to derive a "
            "target token from an unbound/substituted observation (Gate#3 CRITICAL-1: a caller "
            "cannot route a price decided from one admitted token onto a different admitted token)"
        )
    if leg not in decision.intent_plan:
        raise ValueError(
            "build_r4a_request: leg is not part of the bound decision's intent_plan — refusing to "
            "build a request for a leg the reviewed decision never committed to (Gate#3 CRITICAL-1)"
        )

    # DERIVE the target token from the reviewed stream identity — NEVER a caller argument (Gate#3 C-4).
    token_id = observation.stream_identity().token_id
    intent_kind = NEUTRAL_TO_R4A[leg.kind]
    return MMExecutionToolRequest.build(
        intent_kind=intent_kind,
        intent_params=_intent_params(leg, config, token_id),
        strategy_id=config.strategy_id,
        strategy_config_hash=config.strategy_config_hash,
        policy_hash=config.policy_hash,
        session_id=config.session_id,
        manifest_hash=config.manifest_hash,
        evidence_class=_PINNED_EVIDENCE_CLASS,  # PINNED — never a caller/agent param (AC-025)
        mode=config.mode,
        # Admitted pins from the INDEPENDENT authority (Gate #4 C-CRITICAL-1) — NEVER re-read from the
        # declared ``config``, so a caller-selected declared pin cannot self-select the admitted pin.
        admitted_manifest_hash=admitted.manifest_hash,
        admitted_policy_hash=admitted.policy_hash,
        admitted_strategy_config_hash=admitted.strategy_config_hash,
        # reason/confidence deliberately NOT passed: untrusted metadata has zero effect (AC-024).
    )
