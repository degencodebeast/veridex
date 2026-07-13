"""E6-T1/E6-T2 — ``run_dust_execution`` skeleton + submit gates + full lifecycle-event stream
(SAF-007, AC-010/017, AC-003, §6 group 6).

The REAL-fill dust-execution runner's SKELETON and the SAFETY-CORE submit gates. Everything is
INJECTED — the venue ``adapter``, the ``signer`` control plane, the quote ``sources``, the
``now_fn`` / ``sleep_fn`` clocks, the ``envelope`` + ``manifest`` pins, and the execution ``mode``
— so the runner holds no wall-clock, opens no connection, and (in Mode A) places NO orders. This
matches the dust lane's async discipline (injected clocks, deterministic tests, Mode B UNARMED).

Submit gates (the safety core of E6-T1). The runner REFUSES to submit (abstains — no order reaches
the adapter) whenever ANY of the following holds for a token's quote:

* **stale by age** — ``now - quote_ts_s > envelope.max_quote_age_s`` (AC-010);
* **stale / gapped source** — ``sources.read_quote`` raises :class:`StaleVenueBook` (the source is
  disconnected / mid-resync / gapped and refuses to serve a stale book);
* **event-suspended market** — ``quote.event_suspended``;
* **no-quote / boundary state** — ``quote.no_quote``;
* **negative-liquidity book** — a book side with ``size < 0``;
* **missing book side** — a side is absent; it is ABSTAINED, **never imputed / fabricated**.

Only when EVERY gate is clear AND the mode is ``live_guarded`` (Mode B) does the runner build and
submit an order on the wire; in ``dry_run`` (Mode A) a clean quote still places NO order. The
decision telemetry is boolean/id/closed-vocab only — no secret, signer artifact, order, or raw
venue handle ever crosses into :class:`SubmitDecision` (SEC-005 discipline).

E6-T2 (lifecycle-event emission, AC-003). For every GATE-CLEAR quote the runner also builds the
E1-T2 append-only, unique-``sequence_no`` lifecycle stream: a session-identity preamble
(:class:`~veridex.dust_execution.contracts.DustExecutionSessionMeta`, unnumbered — it carries no
``sequence_no``) followed by the numbered stream ``SessionRiskSnapshot -> OrderSubmitIntent ->
OrderSubmitAttempt -> OrderAckEvent -> OrderStatusEvent -> RealFillReconciliation ->
DustRunLabelEvent``. Mode A and Mode B emit the IDENTICAL event TYPES in the IDENTICAL ORDER for
the same input — the ONLY difference is whether a real order moved (Mode A's ``OrderAckEvent``
honestly records ``ack_status="dry_run_not_submitted"`` and a ``None`` venue_order_id instead of
fabricating a real acknowledgement; Mode A still NEVER calls ``adapter.submit_order`` — the E6-T1
``adapter.submit_calls == 0`` / AC-017 invariant is unchanged). A gate-ABSTAINED token emits NO
per-decision lifecycle events — there is no honest order-lifecycle data to record for a decision
that never proceeded past the gate.

SCOPE (E6-T2): the lifecycle-event emission ONLY, over the E6-T1 gate/submit path. The following
remain DELIBERATELY provisional / unwired seams for later E6 tasks (each event field that stands in
for one is flagged PROVISIONAL at its construction site below): the real realized-loss / breaker /
kill-switch accumulator and ``SafetyController`` delegation feeding ``SessionRiskSnapshot`` (E6-T3);
real order-status polling and real venue reconciliation feeding ``OrderStatusEvent`` /
``RealFillReconciliation`` (E6-T3); the Mode A→B arming gate, manifest authorization, and
``resolve_dust_size`` binding + native→decimal pricing (E6-T4 — NOW CLOSED: the wire size comes ONLY
from ``resolve_dust_size`` over pinned inputs, and Mode B arms only when every precondition passes);
a durable operator-assigned ``session_id`` and the sealed ``content_hash`` at session end (E6-T5
closed the startup-sweep half; E6-T6 closed the shutdown cancel-all / explicit leave-open decision
— SEALING ``content_hash`` itself remains a later task); the real EIP-712 V2 order-hash (``venue_order_key``) binding via
``veridex.dust_execution.signing_compiler`` (a later task — this module's placeholder is distinct
from the private integrity digest, never equal to it). E6-T7 CLOSES the final E6 seam: the terminal
lifecycle ``SessionOutcome`` (REQ-014/AC-030) — a bounded, reconciled session is a lifecycle
``"SUCCESS"`` even if it lost money, and ``promoted`` is always ``False`` (no alpha claim is ever
implied). This completes E6.

SEC-003: this module imports only intra-lane ``veridex.dust_execution.*``, the shared
``veridex.policy.envelope`` (the single breach-boundary source of truth, not a ranked lane), and
``veridex.venues.base`` (the pure adapter Protocol/value types) — never ``veridex.live_recorder``
and never a ranked maker/scoring/leaderboard module. :class:`StaleVenueBook` is defined IN-LANE
(the live-recorder lane owns its own same-named exception; this is a copy, not an import).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, cast, runtime_checkable

from veridex.dust_execution.clobv2_gate import Clobv2GateResult
from veridex.dust_execution.contracts import (
    DustExecutionSessionMeta,
    DustRunLabelEvent,
    ExecutionMode,
    OperatorInterlockEvent,
    OrderAckEvent,
    OrderCancelEvent,
    OrderStatus,
    OrderStatusEvent,
    OrderSubmitAttempt,
    OrderSubmitIntent,
    PreSubmitRecord,
    RealFillReconciliation,
    SessionRiskSnapshot,
    TimeInForce,
    UncertainState,
)
from veridex.dust_execution.emergency import (
    CancelAllAdapter,
    DustSafetySession,
    SafetyController,
)
from veridex.dust_execution.facade import IntentKind, MMExecutionToolRequest, MMIntentParams
from veridex.dust_execution.l2_transport import L2SubmitResult
from veridex.dust_execution.manifest import (
    SessionState,
    StrategyAuthorizationDecision,
    StrategyExperimentManifest,
)
from veridex.dust_execution.mode_b_write_port import ModeBWritePort
from veridex.dust_execution.noncrossing import LegKind, OwnOrderLeg, Side, check_non_crossing
from veridex.dust_execution.operator_interlock_store import (
    OperatorInterlockStore,
    interlock_events_are_canonical,
)
from veridex.dust_execution.privy_control_plane import (
    PrivyAuthContext,
    PrivyPreflightResult,
    ProvisioningResult,
    arm_mode_b,
    execute_with,
)
from veridex.dust_execution.reconcile import UncertainSubmitState, assess_uncertain_submit
from veridex.dust_execution.resting_order import RestingOrder
from veridex.dust_execution.risk import FailClosed, RealizedFillRecord, RiskAccumulator
from veridex.dust_execution.signer import Signer, SigningPayload
from veridex.dust_execution.signing_compiler import WireSide
from veridex.dust_execution.sizing import resolve_dust_size
from veridex.dust_execution.wallet_binding import (
    AuthorizationQuorum,
    ExecutionWalletBinding,
    PrivyWalletPolicy,
)
from veridex.policy.circuit_breaker import CircuitBreaker, CircuitState
from veridex.policy.envelope import PolicyEnvelope
from veridex.venues.base import (
    SingleOrderCancelVenue,
    VenueAdapter,
    VenueReconciliationReads,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-lane staleness signal (SEC-003: NOT imported from veridex.live_recorder)
# ---------------------------------------------------------------------------


class StaleVenueBook(Exception):
    """The injected quote source refuses to serve a stale / gapped / disconnected / mid-resync book.

    Mirrors the ``veridex.live_recorder.ws_book_source.StaleVenueBook`` CONCEPT but is defined here,
    in-lane: SEC-003 keeps ``veridex.dust_execution`` isolated from ``veridex.live_recorder``, so the
    source raises THIS exception (a copy, never an import) and the runner catches it as a submit gate.
    """


# ---------------------------------------------------------------------------
# Injected quote-source value types + Protocol (the E1-T2 venue-book read seam)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BookSide:
    """One side of a book: a native ``[0,1]`` price and its resting liquidity ``size``.

    A negative ``size`` is a negative-liquidity book (a submit gate); prices are validated
    downstream by the E5 non-crossing check (consumed by a later E6 task), not here.
    """

    price: float
    size: float


@dataclass(frozen=True)
class DustQuote:
    """A venue-book snapshot for one outcome token, as read from the injected source.

    Attributes:
        token_id: The outcome-token id the quote is for.
        quote_ts_s: Source-clock capture time in integer SECONDS (age is compared against
            ``envelope.max_quote_age_s``, which is also seconds).
        event_suspended: ``True`` when the market is event-suspended (a submit gate).
        no_quote: ``True`` for an explicit no-quote / boundary state (a submit gate).
        bid: The bid side, or ``None`` when absent — a MISSING side is abstained, never imputed.
        ask: The ask side, or ``None`` when absent — a MISSING side is abstained, never imputed.
    """

    token_id: str
    quote_ts_s: int
    event_suspended: bool = False
    no_quote: bool = False
    bid: BookSide | None = None
    ask: BookSide | None = None


@runtime_checkable
class QuoteSource(Protocol):
    """The injected async venue-book read seam (a recording-fake in tests, never a live venue).

    Raises :class:`StaleVenueBook` when the underlying source is gapped / disconnected / mid-resync
    and cannot serve a fresh book — the runner treats that as a submit gate (abstain, no wire).
    """

    async def read_quote(self, token_id: str) -> DustQuote: ...


# ---------------------------------------------------------------------------
# Submit-gate outcome telemetry (boolean / id / closed-vocab ONLY — no secret)
# ---------------------------------------------------------------------------

#: The single closed vocabulary of abstain reasons — boolean-safe, id-free telemetry (SEC-005).
#: ``self_cross`` (E5 non-crossing refusal) and ``safety_blocked`` (E2-T3 emergency-stop block) are
#: the E6-T3 additions — both id-free labels, never an order id or handle.
#: Gate#3 CRITICAL-1 additions — the runner ABSTAINS on the admitted typed intent's own terms:
#: ``intent_no_quote`` (the explicit DON'T-TRADE abstention), ``intent_cancel_all`` (the intent
#: fired the cancel-all sweep, so no NEW order is submitted), ``intent_not_permitted`` (the intent
#: kind is not in ``manifest.permitted_intent_kinds`` — fail-closed), ``intent_params_invalid``
#: (a maker/cancel-replace intent whose typed params are missing/incoherent — fail-closed, no wire),
#: and ``intent_token_mismatch`` (Gate#3 C-4: a SINGULAR order-placing intent whose admitted
#: ``intent_params.token_id`` is not THIS loop token — a singular intent moves ONE admitted token, so
#: every other universe token abstains; a missing / out-of-universe target fails closed, all abstain).
#: Gate#3 M-3 additions — the PRE-SUBMIT order-count cap refusals. ``order_cap_run`` /
#: ``order_cap_session`` / ``order_cap_day`` are the SAME closed-vocab literals the E2 policy gate
#: (:func:`veridex.policy.gate.evaluate_pre_quote`) and engine (``_REASON_ORDER_CAP_*``) emit — the
#: runner reuses them so the durable run/session/UTC-day + ``manifest.max_orders`` caps it now
#: enforces speak one vocabulary end to end (no minted synonyms).
AbstainReason = Literal[
    "stale_quote_age",
    "stale_source",
    "event_suspended",
    "no_quote",
    "missing_book_side",
    "negative_liquidity",
    "self_cross",
    "safety_blocked",
    "mode_a_no_orders",
    "manifest_hash_mismatch",
    "admission_denied",
    "mode_b_not_armed",
    "mode_b_legacy_signer",
    "mode_b_write_port_missing",
    "operator_interlock_unproven",
    "intent_no_quote",
    "intent_cancel_all",
    "intent_not_permitted",
    "intent_params_invalid",
    "intent_token_mismatch",
    "cancel_replace_old_order_live",
    "order_cap_run",
    "order_cap_session",
    "order_cap_day",
]

#: Tuple form of :data:`AbstainReason` for membership checks / iteration.
ABSTAIN_REASONS: tuple[AbstainReason, ...] = (
    "stale_quote_age",
    "stale_source",
    "event_suspended",
    "no_quote",
    "missing_book_side",
    "negative_liquidity",
    "self_cross",
    "safety_blocked",
    "mode_a_no_orders",
    "manifest_hash_mismatch",
    "admission_denied",
    "mode_b_not_armed",
    "mode_b_legacy_signer",
    "mode_b_write_port_missing",
    "operator_interlock_unproven",
    "intent_no_quote",
    "intent_cancel_all",
    "intent_not_permitted",
    "intent_params_invalid",
    "intent_token_mismatch",
    "cancel_replace_old_order_live",
    "order_cap_run",
    "order_cap_session",
    "order_cap_day",
)

#: The venue minimum price increment the non-crossing check rounds/compares against (Polymarket
#: default). E6-T4 will bind the real per-market tick from the resolved market; this is the seam
#: default the runner routes every proposed order through :func:`check_non_crossing` with.
_DEFAULT_TICK_SIZE: float = 0.01

#: Boundary map from the E4 in-code tri-state (underscore) to the persisted-event
#: :data:`~veridex.dust_execution.contracts.UncertainState` (hyphenated ``DEFINITIVELY-ABSENT``).
#: The two spellings are a deliberate boundary (see ``reconcile.UncertainSubmitState``), not a drift.
_RECONCILED_STATE: dict[UncertainSubmitState, UncertainState] = {
    "RESOLVED": "RESOLVED",
    "AMBIGUOUS": "AMBIGUOUS",
    "DEFINITIVELY_ABSENT": "DEFINITIVELY-ABSENT",
}


@dataclass(frozen=True)
class SubmitDecision:
    """The per-token submit/abstain decision — carries ONLY JSON-primitive, non-secret telemetry.

    Never carries a raw order, signer artifact, or venue handle (mirrors the ``facade`` boundary
    discipline): ``abstain_reason`` is a closed-vocabulary label, ``venue_order_id`` a non-secret id.
    """

    token_id: str
    submitted: bool
    abstain_reason: AbstainReason | None
    venue_order_id: str | None = None


@dataclass(frozen=True)
class OperatorInterlockProof:
    """The STORE-ISSUED human-operator interlock proof BOUND into the arming bundle (REQ-005, M-1).

    Gate#3 MAJOR-1 (+ M-1 follow-up): the five REQ-005/006 human preconditions are enforced in the
    facade, but the runner is public/exported, so the arming ARTIFACT the runner consumes must itself
    carry proof that the human interlock was satisfied AND durably PERSISTED — otherwise a direct
    :func:`run_dust_execution` with only the six TECHNICAL conditions would arm real money and bypass
    the human gate. The original fix carried a bool + a SELF-computed ``recording_receipt``, which was
    caller-FORGEABLE: a direct caller could construct ``OperatorInterlockProof(True, "forged")`` and
    arm. The M-1 fix makes the receipt STORE-VERIFIABLE — ``recording_receipt`` must be a receipt an
    injected :class:`~veridex.dust_execution.operator_interlock_store.OperatorInterlockStore` ACTUALLY
    ISSUED for exactly this run's ``(session_id, ordered event content, operator authorization, arming
    attempt)``. The facade is the ONLY sanctioned minter: it constructs this proof solely after
    :func:`~veridex.dust_execution.facade.evaluate_operator_interlock` reports ``armed`` AND the store
    durably RECORDED the events and issued the receipt.

    ``events`` (the recorded REQ-005 audit trail) and ``operator_authorization_ref`` are the
    proof-carried binding CONTENT the runner re-presents to ``store.verify`` (the runner has no other
    source for them); the session identity and arming attempt come from the LIVE run, never the proof.

    Fail-closed by construction: :func:`_mode_b_arming_block_reason` arms ONLY when ``satisfied is
    True`` AND the injected store VERIFIES ``recording_receipt`` against the ACTUAL run session /
    events / operator-auth / arming-attempt; a defaulted/absent proof (``None``), an unsatisfied one,
    a forged/never-issued receipt, or a wrong-session/altered-event/wrong-attempt binding keeps Mode B
    UNARMED. Carries ONLY bools / ids / non-secret refs (SEC-005), never a live handle.
    """

    satisfied: bool
    recording_receipt: str
    #: The recorded REQ-005 audit-trail events this receipt was issued over (ordered content). The
    #: runner re-presents them to ``store.verify`` — an altered/absent set fails to verify. Non-secret
    #: (refs/bools only, SEC-005). Defaults empty so a technical-only/forged bundle carries none.
    events: tuple[OperatorInterlockEvent, ...] = ()
    #: The non-secret operator-authorization ref bound into the receipt (SEC-005); ``None`` when absent.
    operator_authorization_ref: str | None = None


@dataclass(frozen=True)
class ModeBArming:
    """The FULL Mode-B (real-money) arming bundle — every precondition must POSITIVELY pass (E6-T4).

    Modelled as a frozen snapshot so the arming check cannot be partially mutated into a write. Mode B
    arms ONLY when ALL of the following hold (fail-closed AND — a missing/failing member blocks):

    * ``mode_a_passed`` — the HARD GATE: Mode A (fake/dry-run) MUST have passed first;
    * ``clobv2_gate`` — the E3-T5 CLOB-V2 write-contract gate is ``mode_b_admitted`` (machine
      fixture-match AND an operator-confirmed production smoke — ``operator_smoke_ok is True``);
    * ``privy_preflight`` — the E3-T8 operator-run Privy signing preflight passed (``ok is True``);
    * ``provisioning`` — the E3-T8 operator-run pUSD/approvals/gas provisioning passed (``ok is True``);
    * ``binding`` + ``live_policy`` + ``live_quorum`` — a valid :class:`ExecutionWalletBinding` whose
      ``binding_hash`` verifies against the pinned manifest field (:func:`execute_with`) AND whose
      ``privy_policy_content_hash`` + quorum verify against the LIVE policy/quorum
      (:func:`arm_mode_b`). Any mismatch/weakening fails closed.

    ``clobv2_gate`` / ``privy_preflight`` / ``provisioning`` carry the OPERATOR-supplied tri-states
    (``ok=None`` until an operator runs them OUT of CI); offline tests drive each pass/fail with a
    genuine fixture — Mode B stays UNARMED and no live venue/Privy/provisioning call is ever made.
    """

    mode_a_passed: bool
    clobv2_gate: Clobv2GateResult
    privy_preflight: PrivyPreflightResult
    provisioning: ProvisioningResult
    binding: ExecutionWalletBinding
    live_policy: PrivyWalletPolicy
    live_quorum: AuthorizationQuorum
    #: Gate#3 MAJOR-1 (REQ-005): the RECORDED-satisfied human-operator interlock proof. Additive and
    #: fail-closed — ``None`` (the default, e.g. a technical-only bundle built directly against the
    #: runner) keeps Mode B UNARMED. Only the facade mints it, after the five human preconditions are
    #: satisfied AND durably recorded, so the human gate cannot be bypassed via the public runner.
    operator_interlock: OperatorInterlockProof | None = None
    #: Gate#3 C-1 fix (REQ-016/018): the ONE narrow injected Mode-B write port — no default, no
    #: legacy implementation. ``None`` (the default) keeps Mode B UNARMED
    #: (``"mode_b_write_port_missing"``): an armed run may NEVER fall back to the generic
    #: ``adapter.submit_order`` / ``submit_resting_order`` surfaces. Only the production
    #: :class:`~veridex.dust_execution.mode_b_write_port.KeylessModeBWritePort` (or an offline fake
    #: composing the SAME E3-T8 keyless stack) may be injected here.
    write_port: ModeBWritePort | None = None
    #: The signed Privy authorization wrapper (replay-guard expiry + quorum signature set +
    #: idempotency key) threaded to every :meth:`ModeBWritePort.submit_order` call this run makes.
    #: ``None`` (the default) keeps Mode B UNARMED (``"mode_b_write_port_missing"``) — an armed real-
    #: money run must carry a genuine authorization context, never an implicit/omitted one.
    order_auth: PrivyAuthContext | None = None


# ---------------------------------------------------------------------------
# E6-T6: shutdown cancel-all or explicit leave-open decision (SAF-006, AC-009)
# ---------------------------------------------------------------------------

#: The two — and ONLY two — explicit shutdown outcomes (SAF-006). There is deliberately no third
#: value: a shutdown MUST resolve to either sweeping resting orders or an explicit, recorded choice
#: to leave them resting; a silent abandon is not a representable state in this closed vocabulary.
#: ``"leave_open"`` is the honest name for the no-cancel branch (Gate#3 MINOR-1): that branch leaves
#: resting orders OPEN — it never means "no exposure," so it is never named ``"leave_flat"``.
ShutdownPolicy = Literal["cancel_all", "leave_open"]


@dataclass(frozen=True)
class ShutdownDecision:
    """The explicit SAF-006 shutdown outcome — carries ONLY JSON-primitive telemetry, never silent.

    Recorded on every :class:`DustExecutionResult`, in BOTH modes, so a run can never end without an
    explicit, inspectable shutdown record. ``policy`` is the resolved :data:`ShutdownPolicy`;
    ``cancel_all_fired`` is ``True`` ONLY when THIS shutdown call actually routed a FRESH sweep
    through the E2-T3 :meth:`~veridex.dust_execution.emergency.SafetyController.cancel_all_and_block`
    primitive and fired the venue wire (Mode B only — Mode A places no orders, so there is nothing to
    sweep, AC-017). A ``"leave_open"`` policy always carries ``cancel_all_fired=False`` — the explicit
    choice to leave resting orders open, never an omission.

    ``already_satisfied_by_prior_sweep`` (Gate#3 MINOR-1) distinguishes the idempotent case: when an
    EARLIER safety trigger (breaker / loss-breach / kill-switch / startup-sweep) already fired the
    cancel-all wire and blocked the session, a ``"cancel_all"`` shutdown call is a wire NO-OP — it must
    NOT claim ``cancel_all_fired=True`` for a sweep it did not perform. In that case
    ``cancel_all_fired`` is ``False`` and ``already_satisfied_by_prior_sweep`` is ``True``, honestly
    reporting "the cancel-all outcome already holds, but this call touched no wire." For a
    ``"leave_open"`` policy (which never attempts a sweep) ``already_satisfied_by_prior_sweep`` is
    always ``False`` — it is only meaningful relative to a ``"cancel_all"`` attempt.
    """

    policy: ShutdownPolicy
    cancel_all_fired: bool
    already_satisfied_by_prior_sweep: bool


# ---------------------------------------------------------------------------
# E6-T7: terminal lifecycle status — a bounded, reconciled session is a SUCCESS
# EVEN IF IT LOST MONEY; never promoted strategy evidence (REQ-014, AC-030).
# ---------------------------------------------------------------------------

#: The two — and ONLY two — terminal lifecycle outcomes (REQ-014). Derived PURELY from the SAFETY
#: outcome (loss caps / breaker / kill-switch / reconciliation) — NEVER from ``realized_pnl`` sign.
SessionStatus = Literal["SUCCESS", "FAILED"]


@dataclass(frozen=True)
class SessionOutcome:
    """The REQ-014/AC-030 terminal lifecycle outcome — SAFETY-derived, NEVER PnL-sign-derived.

    R4-A proves SAFETY, not alpha (§6 group 13). A dust session that stays within its loss caps AND
    reconciles cleanly against venue truth is a lifecycle ``"SUCCESS"`` even when it realized a
    NEGATIVE PnL — a losing dust session is the EXPECTED, honest outcome of a strategy-neutral
    safety proof, and a negative PnL must NEVER flip ``status`` to ``"FAILED"``. ``status`` flips to
    ``"FAILED"`` ONLY on an actual SAFETY failure: a realized-loss-cap breach (SAF-002d, via the SAME
    ``RiskAccumulator.breaches_caps`` predicate the emergency-stop sweep uses — ONE source of truth),
    an open circuit breaker, an engaged kill switch, or an unresolved/frozen (``AMBIGUOUS``)
    reconciliation against venue truth — never on losing money.

    ``promoted`` is ALWAYS ``False``: this lane proves safety, never an alpha claim (mirrors the
    pinned ``DustRunLabelEvent.edge_label == "NOT_PROVEN_EDGE"``), so no dust-execution session —
    winning or losing, ``SUCCESS`` or ``FAILED`` — is ever marked as promoted strategy evidence. A
    lifecycle ``"SUCCESS"`` proves the run was SAFE, not that an edge was proven; the two properties
    are deliberately distinct and computed independently.
    """

    status: SessionStatus
    promoted: bool


def _resolve_session_outcome(
    *,
    risk: RiskAccumulator,
    envelope: PolicyEnvelope,
    breaker: CircuitBreaker | None,
    events: Sequence[LifecycleEvent],
) -> SessionOutcome:
    """Derive the terminal :class:`SessionOutcome` from the SAFETY outcome, never from PnL sign.

    "Bounded": the fee-inclusive realized loss folded from the real, E2-T2-ledger-sourced
    ``RealizedFillRecord`` fills has not reached either ENABLED loss cap (:meth:`RiskAccumulator.
    breaches_caps` — the SAME predicate :func:`_apply_safety_triggers` uses to fire the emergency
    sweep, so the two can never drift), the circuit breaker is not OPEN, and the kill switch is not
    engaged. "Reconciled": no per-decision :class:`RealFillReconciliation` for a decision that
    ACTUALLY SUBMITTED a real order to the wire was left ``AMBIGUOUS`` (unresolved/frozen) against
    venue truth. A reconciliation is a genuine SAFETY FREEZE only when a real order reached the wire
    for that decision — a submitted-but-unconfirmed fund state. A Mode A ``dry_run`` (or an abstained
    token) places NO order yet still emits a per-decision ``AMBIGUOUS`` reconciliation (there is no
    real venue fill to confirm); "nothing was submitted, nothing to reconcile" is benign and must NOT
    freeze the session — it is not the "submitted but its fill is unconfirmed" state a freeze exists
    to flag (Gate#3 MINOR-1). The freeze is correlated per-decision through the honest
    :class:`~veridex.dust_execution.contracts.OrderAckEvent`: ``ack_status == "dry_run_not_submitted"``
    marks that NO wire was touched for that decision, so only an ack that actually submitted
    (``"accepted"`` / ``"not_accepted"``) can make its ``decision_id``'s reconciliation a freeze. A
    bounded, reconciled session is ``"SUCCESS"`` regardless of whether the accumulated ``realized_pnl``
    was positive or negative — a losing-but-bounded run IS the honest safety proof this lane exists to
    produce. ``promoted`` is ALWAYS ``False`` (see :class:`SessionOutcome`) — no alpha claim is ever
    implied.
    """
    # The decision_ids that ACTUALLY submitted a real order to the wire. ``OrderAckEvent.ack_status``
    # is the honest per-decision marker: only Mode B's real submit records ``"accepted"`` /
    # ``"not_accepted"`` (the order reached the wire); Mode A records ``"dry_run_not_submitted"`` (no
    # wire touched), which never counts toward a freeze.
    submitted_decision_ids = {
        event.decision_id
        for event in events
        if isinstance(event, OrderAckEvent) and event.ack_status != "dry_run_not_submitted"
    }
    # A reconciliation is FROZEN only when it is AMBIGUOUS AND a real order was submitted for that
    # decision — a no-submit AMBIGUOUS (dry-run / abstained) is "nothing to reconcile", not a freeze.
    reconciliation_frozen = any(
        isinstance(event, RealFillReconciliation)
        and event.reconciled_state == "AMBIGUOUS"
        and event.decision_id in submitted_decision_ids
        for event in events
    )
    safety_failed = (
        risk.breaches_caps(envelope)
        or (breaker is not None and breaker.state is CircuitState.OPEN)
        or envelope.kill_switch
        or reconciliation_frozen
    )
    status: SessionStatus = "FAILED" if safety_failed else "SUCCESS"
    return SessionOutcome(status=status, promoted=False)


#: The E1-T2 numbered lifecycle-event union this runner emits (session meta precedes it, unnumbered
#: — :class:`DustExecutionSessionMeta` carries no ``sequence_no``). Ordered per event, one variant
#: per stage: risk snapshot, intent, attempt, ack, status, fill/reconciliation, labels.
LifecycleEvent = (
    SessionRiskSnapshot
    | OrderSubmitIntent
    | OrderSubmitAttempt
    | OrderAckEvent
    | OrderCancelEvent
    | OrderStatusEvent
    | RealFillReconciliation
    | DustRunLabelEvent
)


@dataclass(frozen=True)
class DustExecutionResult:
    """The result of one dust-execution pass over the manifest universe.

    ``session_meta`` is the unnumbered session-identity preamble; ``events`` is the append-only,
    unique/monotonic-``sequence_no`` E1-T2 lifecycle stream that follows it. Mode A and Mode B emit
    IDENTICAL event TYPES in IDENTICAL ORDER for the same input (AC-003) — only the recorded DATA
    (e.g. ``ack_status`` / ``venue_order_id``) differs, reflecting whether a real order moved.

    ``shutdown_decision`` is the SAF-006 explicit end-of-run outcome (E6-T6): either the cancel-all
    wire fired or an explicit leave-open choice was recorded — NEVER a silent abandon of
    resting orders. Always populated, in both modes.

    ``session_outcome`` is the REQ-014/AC-030 terminal lifecycle status (E6-T7): a
    :class:`SessionOutcome` derived PURELY from the SAFETY outcome (loss caps / breaker /
    kill-switch / reconciliation) — a bounded, reconciled session is a ``"SUCCESS"`` even if it lost
    money, and ``promoted`` is always ``False`` (no alpha claim is ever implied). Always populated,
    in both modes.
    """

    mode: ExecutionMode
    decisions: tuple[SubmitDecision, ...]
    session_meta: DustExecutionSessionMeta
    events: tuple[LifecycleEvent, ...]
    admission: StrategyAuthorizationDecision
    shutdown_decision: ShutdownDecision
    session_outcome: SessionOutcome

    @property
    def submitted_count(self) -> int:
        """How many decisions actually reached the submit wire (0 in Mode A)."""
        return sum(1 for d in self.decisions if d.submitted)

    @property
    def abstained_count(self) -> int:
        """How many decisions abstained (did NOT submit)."""
        return sum(1 for d in self.decisions if not d.submitted)


# ---------------------------------------------------------------------------
# The submit gate: pure, deterministic, fail-closed to abstain
# ---------------------------------------------------------------------------


def _evaluate_submit_gate(quote: DustQuote, *, now_s: int, max_quote_age_s: int) -> AbstainReason | None:
    """Return the abstain reason gating this quote, or ``None`` when EVERY gate is clear.

    Order is chosen so the most structural refusals report first, but ALL of them abstain (no order
    reaches the wire). A missing book side returns ``"missing_book_side"`` and is NEVER imputed — the
    absent side is not fabricated to let the quote through.
    """
    if quote.event_suspended:
        return "event_suspended"
    if quote.no_quote:
        return "no_quote"
    if quote.bid is None or quote.ask is None:
        # A missing side is ABSTAINED, never imputed/fabricated (AC-017).
        return "missing_book_side"
    if quote.bid.size < 0.0 or quote.ask.size < 0.0:
        return "negative_liquidity"
    # Staleness-by-age gate (AC-010) — THE mutation target. ``max_quote_age_s`` and ``quote_ts_s``
    # are both integer seconds; strictly-greater-than age fails closed to abstain.
    if now_s - quote.quote_ts_s > max_quote_age_s:
        return "stale_quote_age"
    return None


# ---------------------------------------------------------------------------
# Deterministic, monotonic sequence_no allocation (E6-T2, AC-003)
# ---------------------------------------------------------------------------


class _SeqCounter:
    """Deterministic, monotonic ``sequence_no`` allocator for one run's lifecycle stream.

    Starts at ``1`` and increments by exactly ``1`` per call — append-only, unique, gap-free by
    construction. Not a randomness/clock seam: purely arithmetic, so it needs no injection.
    """

    def __init__(self) -> None:
        self._next = 1

    def next(self) -> int:
        """Return the next ``sequence_no`` and advance the counter."""
        n = self._next
        self._next += 1
        return n


# ---------------------------------------------------------------------------
# Session-level event builders (preamble + once-per-run stages)
# ---------------------------------------------------------------------------


def provisional_session_id(manifest: StrategyExperimentManifest, mode: ExecutionMode) -> str:
    """The PROVISIONAL session identity the runner runs under — derived from ``(strategy_id, mode)``.

    The single source of the derived session id: both the session-meta preamble AND the facade's
    operator-interlock recording bind to THIS value, so the receipt the store issues at record time
    is bound to exactly the ``session.session_id`` the runner verifies against (a durable,
    operator-assigned identity replaces this seam in a later task).
    """
    return f"{manifest.strategy_id}:{mode}"


def _build_session_meta(
    *,
    manifest: StrategyExperimentManifest,
    envelope: PolicyEnvelope,
    signer: Signer,
    mode: ExecutionMode,
    session_identity: str | None = None,
) -> DustExecutionSessionMeta:
    """Session identity/provenance preamble (unnumbered — carries no ``sequence_no``).

    ``session_id`` is the AUTHORITATIVE, operator-assigned IMMUTABLE ``session_identity`` when one is
    threaded in (Gate#3 MAJOR-2 — the facade supplies it from its durable session-state provider so
    the safety/ledger join is the real identity, not the provisional seam). Absent one (a direct /
    self-driven runner call), it falls back to the PROVISIONAL ``(strategy_id, mode)`` derivation —
    the sealed ``content_hash`` is still wired by later tasks (E6-T6 shutdown). ``wallet_ref`` is the
    signer's own non-secret provider label (never a key/address). Every other field is REAL, sourced
    directly from the pinned ``manifest`` / ``envelope``.
    """
    return DustExecutionSessionMeta(
        session_id=(
            session_identity if session_identity is not None else provisional_session_id(manifest, mode)
        ),
        mode=mode,
        wallet_ref=signer.mode,
        manifest_hash=manifest.manifest_hash(),
        policy_hash=envelope.policy_hash(),
        caps_snapshot={
            "max_orders": float(manifest.max_orders),
            "max_notional": manifest.max_notional,
            "max_session_loss": manifest.max_session_loss,
            "max_daily_loss": manifest.max_daily_loss,
        },
        market_fee_snapshot_hash=manifest.market_fee_snapshot_hash,
        operator_authorization_ref=manifest.operator_authorization,
        partial_content_hash=None,
        content_hash=None,  # PROVISIONAL — sealed at session end: later task (E6-T6 shutdown)
    )


def _build_risk_snapshot(
    *,
    seq: int,
    now_ms: int,
    envelope: PolicyEnvelope,
    risk: RiskAccumulator,
    breaker: CircuitBreaker | None,
    open_order_count: int,
) -> SessionRiskSnapshot:
    """Session-level risk snapshot (``decision_id=None``) — first event in the numbered stream.

    E6-T3 closes the E6-T2 PROVISIONAL risk seam: ``realized_loss_session/daily`` now carry the REAL
    fee-inclusive loss from the threaded :class:`~veridex.dust_execution.risk.RiskAccumulator` (fed
    from real venue-reconciled fills BEFORE this snapshot is built), ``breaker_open`` reflects the
    injected :class:`~veridex.policy.circuit_breaker.CircuitBreaker` state, and ``open_order_count``
    the runner's count of own resting legs. ``kill_switch_engaged`` was already real
    (``envelope.kill_switch``). Every value is honest — never a fabricated non-zero.
    """
    return SessionRiskSnapshot(
        sequence_no=seq,
        event_type="SessionRiskSnapshot",
        source_ts=None,
        recv_ts=now_ms,
        decision_id=None,
        realized_loss_session=risk.realized_loss_session,
        realized_loss_daily=risk.realized_loss_day,
        open_order_count=open_order_count,
        breaker_open=breaker is not None and breaker.state is CircuitState.OPEN,
        kill_switch_engaged=envelope.kill_switch,
    )


def _build_label_event(*, seq: int, now_ms: int, manifest: StrategyExperimentManifest) -> DustRunLabelEvent:
    """Mandatory honesty labels (AC-025) — last event in the numbered stream, once per run."""
    return DustRunLabelEvent(
        sequence_no=seq,
        event_type="DustRunLabelEvent",
        source_ts=None,
        recv_ts=now_ms,
        run_label="DUST_LIVE",
        evidence_class=manifest.evidence_class,
        calibration_label="UNCALIBRATED",
        edge_label="NOT_PROVEN_EDGE",
    )


# ---------------------------------------------------------------------------
# Emergency-stop delegation: the runner DELEGATES every trigger to the E2-T3
# SafetyController's single idempotent cancel_all_and_block primitive (SAF-002/003).
# ---------------------------------------------------------------------------


def _require_cancel_all_adapter(adapter: VenueAdapter) -> CancelAllAdapter:
    """Return the venue adapter as a :class:`CancelAllAdapter`, failing closed if it cannot sweep.

    A safety trigger that cannot fire the venue ``cancel_all_orders`` sweep is a fatal wiring error
    on the real-money path — blocking without sweeping would leave resting orders exposed. Fail
    closed (raise) rather than silently skip the sweep.
    """
    if not isinstance(adapter, CancelAllAdapter):
        raise TypeError(
            "a safety trigger fired but the venue adapter cannot sweep resting orders "
            "(missing cancel_all_orders); refusing to block-without-sweep"
        )
    return adapter


def _require_single_cancel_venue(adapter: VenueAdapter) -> SingleOrderCancelVenue:
    """Return the adapter as a :class:`SingleOrderCancelVenue`, failing closed if it cannot cancel one.

    A ``cancel_replace`` intent that cannot reach the E3-T4 single-order ``DELETE /order`` wire
    (``cancel_single_order``) is a fatal wiring error — fail closed (raise) rather than place the
    replacement WITHOUT cancelling the named order it is meant to replace.
    """
    if not isinstance(adapter, SingleOrderCancelVenue):
        raise TypeError(
            "a cancel_replace intent was admitted but the venue adapter cannot cancel a single order "
            "(missing cancel_single_order); refusing to replace-without-cancel"
        )
    return adapter


async def _apply_safety_triggers(
    *,
    adapter: VenueAdapter,
    safety: SafetyController,
    session: DustSafetySession,
    risk: RiskAccumulator,
    envelope: PolicyEnvelope,
    breaker: CircuitBreaker | None,
    realized_fills: Sequence[RealizedFillRecord],
) -> None:
    """Fold real fills into risk and DELEGATE every runner-reachable trigger to the SafetyController.

    The runner does NOT reimplement cancel-all: each trigger routes through the E2-T3 single
    idempotent :meth:`~veridex.dust_execution.emergency.SafetyController.cancel_all_and_block`
    primitive (via its typed ``on_*`` entry points), which fires the venue ``cancel_all_orders`` wire
    AND sets the submit-block flag. Triggers handled here (each idempotent — the first blocks, the
    rest are no-ops that stay blocked):

    * **realized-loss-cap breach** — every real ``RealizedFillRecord`` is folded through
      :meth:`SafetyController.on_realized_fill`, which accumulates fee-inclusive loss and, on a cap
      crossing, ATOMICALLY blocks + sweeps under the honest ``loss_breach`` cause;
    * **breaker-open** — an OPEN :class:`CircuitBreaker` delegates to :meth:`on_breaker_open`;
    * **kill-switch** — ``envelope.kill_switch`` delegates to :meth:`on_kill_switch`.

    Fills are folded FIRST so the following :func:`_build_risk_snapshot` carries the real loss.
    """
    for fill in realized_fills:
        await safety.on_realized_fill(
            fill,
            adapter=_require_cancel_all_adapter(adapter),
            session=session,
            risk=risk,
            envelope=envelope,
        )
    if breaker is not None and breaker.state is CircuitState.OPEN:
        await safety.on_breaker_open(adapter=_require_cancel_all_adapter(adapter), session=session)
    if envelope.kill_switch:
        await safety.on_kill_switch(adapter=_require_cancel_all_adapter(adapter), session=session)


async def _read_startup_open_orders(adapter: VenueAdapter) -> tuple[bool, list[object]]:
    """Read the isolated wallet's open orders for the SAF-005 startup sweep, distinguishing a
    TRUSTWORTHY zero-orders read from an UNKNOWN / unavailable one (Gate#3 MAJOR-2).

    The E3-T2 ``get_orders`` reconciliation surface returns the FLATTENED bare ``list`` of open-order
    records — the adapter iterates the §5 cursor pages (``next_cursor``: ``MA==`` first, ``LTE=``
    terminal) itself. Returns ``(read_ok, orders)``:

    * ``read_ok is True`` ONLY for a SUCCESSFUL, well-shaped read: a bare ``list`` (an EMPTY list is
      trustworthy proof of zero open exposure — the positive control that still permits submit).
    * ``read_ok is False`` — UNKNOWN exposure, NEVER treated as proof of absence — when the read
      surface is ABSENT, RAISES, or returns a MALFORMED / INCOMPLETE-PAGINATED shape (anything that is
      not a bare ``list``: a ``None`` / scalar, or a partial page ENVELOPE dict that leaked instead of
      the flattened list — the truth is incomplete because more results were not fetched).

    Degrading a FAILED read to "zero open orders" would be fail-OPEN for the submit decision (unknown
    exposure treated as PROOF of no exposure); this function refuses to do that. No order payload is
    logged (SEC-005) — only the boolean truth of whether a trustworthy read was obtained.
    """
    getter = getattr(adapter, "get_orders", None)
    if getter is None:
        logger.debug("startup get_orders read surface ABSENT; open-order truth is UNKNOWN")
        return False, []
    try:
        response = await getter()
    except Exception:
        # A raised read is UNKNOWN exposure — never manufacture proof of absence; no payload logged.
        logger.debug("startup get_orders read FAILED (raised); open-order truth is UNKNOWN", exc_info=True)
        return False, []
    if not isinstance(response, list):
        # A malformed / incomplete-paginated shape (non-list: None/scalar, or a leaked partial page
        # envelope) is UNKNOWN — the flattened bare-list read contract was not satisfied.
        logger.debug("startup get_orders returned a MALFORMED/INCOMPLETE shape; open-order truth is UNKNOWN")
        return False, []
    return True, list(response)


async def _startup_open_order_sweep(
    *,
    adapter: VenueAdapter,
    safety: SafetyController,
    session: DustSafetySession,
    enforce: bool,
) -> int:
    """SAF-005 startup sweep: reconcile/cancel the isolated wallet's PRE-EXISTING open orders on arm.

    On arm, the runner cannot BLINDLY submit into pre-existing exposure: it MUST first OBTAIN a
    TRUSTWORTHY read of the venue's own open orders and reconcile/cancel them BEFORE any submit. This
    runs BEFORE the token loop so no order is ever placed atop a pre-existing resting order.

    QUERY (both modes) — via :func:`_read_startup_open_orders`, which distinguishes three cases (the
    Gate#3 MAJOR-2 fix): a SUCCESSFUL read returning ZERO orders (trustworthy — permits submit); a
    SUCCESSFUL read returning ≥1 order (sweep + block); and a read-FAILED/UNKNOWN result — an ABSENT /
    RAISING / MALFORMED / INCOMPLETE-PAGINATED read. UNKNOWN exposure is NEVER degraded to "zero open
    orders": doing so would be fail-OPEN (treating unknown exposure as PROOF of no exposure). No order
    payload is logged (SEC-005).

    CANCEL / BLOCK (armed Mode B only — the "on arm" moment, ``enforce`` True). A ≥1-order read
    DELEGATES to the E2-T3 single idempotent
    :meth:`~veridex.dust_execution.emergency.SafetyController.cancel_all_and_block` (it does NOT
    reimplement cancel): the venue ``cancel_all_orders`` wire is FIRED and submits are BLOCKED. An
    UNKNOWN read ALWAYS blocks too (Gate#3 M-2), INDEPENDENT of any cancel-all surface: a sweep-capable
    (:class:`~veridex.dust_execution.emergency.CancelAllAdapter`) adapter sweeps + blocks via the SAME
    primitive; a NON-sweep-capable adapter has no wire to fire, so the session submit-block is set
    DIRECTLY (fail closed — operator intervention required, never a test-only permit path). Either way
    the token loop's :meth:`SafetyController.check_can_submit` gate then abstains every token
    ``"safety_blocked"`` — nothing lands atop pre-existing OR possibly-existing exposure. Only a
    SUCCESSFUL read returning ZERO orders leaves submit permitted. (The ≥1-order path still requires a
    sweepable adapter via :func:`_require_cancel_all_adapter`.)

    The sweep is labelled ``"manual"`` — the closest honest cause in the closed
    :data:`~veridex.dust_execution.contracts.CancelAllCause` vocab (an operator-initiated ARM-time
    reconcile, not an automated breaker/loss/timeout reaction). A dedicated startup-sweep cause is a
    later ``contracts.py`` addition, out of E6-T5 scope.

    In Mode A (``enforce`` False) the read is still EXERCISED — the SAF-005 contract is wired, an
    UNKNOWN read is RECORDED (debug) — but NO cancel wire is touched: dry-run places no orders, so
    there is no submit to protect (AC-017), and an unavailable read never causes money I/O or a crash.

    Returns the count of pre-existing open orders observed for a TRUSTWORTHY read, or ``-1`` — the
    read-UNKNOWN sentinel — when the open-order truth could not be obtained (absent/raising/malformed/
    incomplete). The caller drives its decision through the ``cancel_all_and_block`` side effect above,
    not this value.
    """
    read_ok, open_orders = await _read_startup_open_orders(adapter)
    if not read_ok:
        # UNKNOWN exposure (Gate#3 MAJOR-2 / M-2). A fund-touching runner must FAIL CLOSED itself: in
        # an ARMED run (``enforce`` True) unknown startup open-order truth ALWAYS blocks submit,
        # INDEPENDENT of any cancel-all surface — never retain a test-only permit path. Two armed
        # sub-cases:
        #   * SWEEP-CAPABLE adapter — sweep + block: DELEGATE to the single idempotent
        #     ``cancel_all_and_block`` primitive (fires the ``cancel_all_orders`` wire AND blocks).
        #   * NON-sweep-capable adapter — block WITHOUT a wire: there is no cancel-all surface to fire,
        #     but unknown exposure is still a fund hazard, so set the session submit-block DIRECTLY
        #     (operator intervention required) rather than submit atop possibly-existing exposure.
        # Either way the token loop's ``check_can_submit`` gate then abstains every token
        # ``"safety_blocked"``. Mode A (``enforce`` False) records the unavailable read but touches NO
        # wire and does NOT block (dry-run has no submit to protect, AC-017) — an unavailable read
        # never causes money I/O or a crash. No order payload is logged (SEC-005): boolean truth only.
        if enforce:
            if isinstance(adapter, CancelAllAdapter):
                await safety.cancel_all_and_block("manual", adapter=adapter, session=session)
                logger.warning(
                    "dust_execution.startup_sweep",
                    extra={"open_order_truth": "unknown", "swept": True, "blocked": True},
                )
            else:
                # No wire to fire — fail closed by setting the submit-block directly (idempotent; a
                # prior trigger may already have blocked). "manual" is the honest ARM-time cause.
                session.submit_blocked = True
                session.block_cause = "manual"
                logger.warning(
                    "dust_execution.startup_sweep",
                    extra={
                        "open_order_truth": "unknown",
                        "swept": False,
                        "blocked": True,
                        "sweep_capable": False,
                    },
                )
        else:
            logger.info(
                "dust_execution.startup_sweep",
                extra={
                    "open_order_truth": "unknown",
                    "swept": False,
                    "blocked": False,
                    "sweep_capable": isinstance(adapter, CancelAllAdapter),
                },
            )
        return -1
    count = len(open_orders)
    if enforce and count > 0:
        # Reuse the single idempotent cancel primitive; fail closed if the adapter cannot sweep.
        await safety.cancel_all_and_block(
            "manual", adapter=_require_cancel_all_adapter(adapter), session=session
        )
        logger.info("dust_execution.startup_sweep", extra={"open_order_count": count, "swept": True})
    return count


async def _apply_shutdown_decision(
    *,
    adapter: VenueAdapter,
    safety: SafetyController,
    session: DustSafetySession,
    mode: ExecutionMode,
    shutdown_policy: ShutdownPolicy,
) -> ShutdownDecision:
    """SAF-006/AC-009: shutdown resolves to exactly one EXPLICIT outcome — never a silent abandon.

    Runs at the END of the run (after the token loop, before the run returns): either the cancel-all
    WIRE fires — DELEGATING to the SAME E2-T3 idempotent
    :meth:`~veridex.dust_execution.emergency.SafetyController.cancel_all_and_block` primitive under
    the ``"shutdown"`` cause (already a member of the closed
    :data:`~veridex.dust_execution.contracts.CancelAllCause` vocab; no per-trigger cancel logic is
    reimplemented here) — OR an EXPLICIT ``"leave_open"`` policy decision is recorded. There is no
    third path: a run ending with resting orders and NEITHER a fired cancel-all NOR a recorded
    decision is the silent-abandon failure this function exists to close.

    ``shutdown_policy == "cancel_all"`` touches the real wire ONLY in Mode B (``live_guarded``); Mode A
    (dry-run) never places an order (AC-017), so there is nothing to sweep — the SAME explicit decision
    contract is still recorded (mirrors the AC-003 / E6-T5 startup-sweep discipline: identical recorded
    outcome shape in both modes, only Mode B touches the wire). ``shutdown_policy == "leave_open"``
    never touches the wire in either mode — an explicit, recorded choice to leave resting orders open.

    Idempotent by construction: if a prior safety trigger (breaker/loss/kill-switch/startup-sweep)
    already blocked the session, a ``"cancel_all"`` shutdown routes through the SAME idempotent
    primitive and is a no-op on the wire (:meth:`SafetyController.cancel_all_and_block` never re-fires
    once blocked) — the shutdown decision is still explicitly returned. Gate#3 MINOR-1: that no-op
    case must NOT be reported as "this shutdown fired the wire" — the block state is read BEFORE
    delegating, so the returned :class:`ShutdownDecision` honestly distinguishes a FRESH wire fire
    (``cancel_all_fired=True``) from an outcome already satisfied by an earlier sweep
    (``cancel_all_fired=False``, ``already_satisfied_by_prior_sweep=True``).

    Returns:
        The :class:`ShutdownDecision` recording the resolved policy, whether THIS call fired the
        cancel-all wire, and whether the outcome was already satisfied by a prior sweep.
    """
    if shutdown_policy == "cancel_all" and mode == "live_guarded":
        # Read the block state BEFORE delegating: cancel_all_and_block is idempotent and will not
        # re-fire the wire if a prior trigger already blocked the session (SafetyController semantics).
        # Capturing this beforehand is the only way to honestly tell "this call fired the wire" apart
        # from "this call found the outcome already satisfied."
        already_satisfied = session.submit_blocked
        await safety.cancel_all_and_block(
            "shutdown", adapter=_require_cancel_all_adapter(adapter), session=session
        )
        cancel_all_fired = not already_satisfied
        logger.info(
            "dust_execution.shutdown",
            extra={
                "shutdown_policy": "cancel_all",
                "mode": mode,
                "cancel_all_fired": cancel_all_fired,
                "already_satisfied_by_prior_sweep": already_satisfied,
            },
        )
        return ShutdownDecision(
            policy="cancel_all",
            cancel_all_fired=cancel_all_fired,
            already_satisfied_by_prior_sweep=already_satisfied,
        )
    logger.info("dust_execution.shutdown", extra={"shutdown_policy": shutdown_policy, "mode": mode})
    return ShutdownDecision(
        policy=shutdown_policy, cancel_all_fired=False, already_satisfied_by_prior_sweep=False
    )


def _non_crossing_gate(
    proposed: OwnOrderLeg, *, own_legs: Sequence[OwnOrderLeg], tick_size: float
) -> AbstainReason | None:
    """Return ``"self_cross"`` if the ``proposed`` order self-crosses an own leg, else ``None`` (E5).

    Gate#3 C-2 (SAF-009): ``proposed`` is the EXACT typed order about to reach the wire — its real
    token_id, side, and native price (a SELL make_quote, a taker crossing side, or a cancel_replace
    replacement), NEVER a phantom hardcoded BUY at the venue ask. It routes THROUGH the pure E5
    :func:`~veridex.dust_execution.noncrossing.check_non_crossing` over the possibly-live union of the
    runner's own resting legs plus this proposed leg. A REJECT verdict refuses the submit; this is the
    wire the submit path must not bypass (mutation: re-hardcode ``proposed`` to a BUY@ask phantom and a
    crossing SELL slips onto the book).
    """
    verdict = check_non_crossing((*own_legs, proposed), tick_size=tick_size)
    return None if verdict.admitted else "self_cross"


# ---------------------------------------------------------------------------
# E6-T4: mechanical size, native→decimal price, manifest authorization, Mode A→B arming
# ---------------------------------------------------------------------------


def _resolve_wire_size(
    *,
    manifest: StrategyExperimentManifest,
    envelope: PolicyEnvelope,
    wallet_equity_at_decision: float,
    fixed_fraction: float,
) -> float:
    """The ONE source of the executable wire size: :func:`resolve_dust_size` and NOTHING else.

    Deterministic ``fixed_fraction * wallet_equity_at_decision`` clamped by the manifest notional cap
    AND the per-order policy cap (the tighter live-guarded cap when enabled, else ``max_stake``). No
    agent ``confidence`` / requested ``size`` is an input here — an agent value can never RAISE or move
    the executable size (GUD-001, Codex-M4). This is the "check the value that FEEDS the wire" binding.
    """
    max_per_order = (
        envelope.max_stake_live_guarded
        if envelope.max_stake_live_guarded > 0.0
        else envelope.max_stake
    )
    return resolve_dust_size(
        fixed_fraction=fixed_fraction,
        wallet_equity_at_decision=wallet_equity_at_decision,
        max_notional=manifest.max_notional,
        max_per_order=max_per_order,
    )


def _native_to_decimal_odds(native_price: float) -> float:
    """Convert a native ``(0,1]`` probability price to decimal odds (``1 / native``), fail-closed.

    The dust lane's native price is a Polymarket-style probability in the unit interval (validated by
    :class:`~veridex.dust_execution.signer.SigningPayload`); decimal odds are its reciprocal. A
    non-finite or non-positive native price is a nonsensical cost-to-fill and fails closed rather than
    emit a garbage (or infinite) decimal price on the wire.
    """
    if not (native_price > 0.0) or native_price > 1.0:
        raise ValueError(
            f"native price must be a probability in (0, 1] to convert to decimal odds, got {native_price!r}"
        )
    return 1.0 / native_price


def _build_admission(
    *,
    manifest: StrategyExperimentManifest,
    envelope: PolicyEnvelope,
    risk: RiskAccumulator,
    breaker: CircuitBreaker | None,
    session_id: str,
    open_order_count: int,
) -> StrategyAuthorizationDecision:
    """Deterministic pre-submit admission (E1-T2/E2-T1) — a pure function of manifest + policy + session.

    Delegates to :meth:`StrategyAuthorizationDecision.evaluate`, which admits an ``EXPERIMENTAL_DUST``
    manifest WITHOUT any profitability flag yet still DENYs on a reached loss cap / breaker / kill
    switch. It is MODE-INDEPENDENT: identical ``(manifest, policy_hash, session)`` → identical verdict
    in dry-run and live-guarded (AC-021), so it is computed once per run and surfaced on the result.
    """
    return StrategyAuthorizationDecision.evaluate(
        manifest=manifest,
        policy_hash=envelope.policy_hash(),
        session=SessionState(
            session_id=session_id,
            realized_loss_session=risk.realized_loss_session,
            realized_loss_daily=risk.realized_loss_day,
            open_order_count=open_order_count,
            breaker_open=breaker is not None and breaker.state is CircuitState.OPEN,
            kill_switch_engaged=envelope.kill_switch,
        ),
    )


def _authorization_block_reason(
    *,
    manifest: StrategyExperimentManifest,
    envelope: PolicyEnvelope,
    request: MMExecutionToolRequest | None,
    admission: StrategyAuthorizationDecision,
) -> AbstainReason | None:
    """Session-level, mode-independent authorization gate (fail-closed to abstain).

    Two checks:

    * **Declared-hash cross-check.** When an agent ``request`` is supplied, its DECLARED
      ``manifest_hash`` / ``policy_hash`` / ``strategy_config_hash`` MUST match the admitted pins; a
      mismatch fails closed (``"manifest_hash_mismatch"``) so an approved intent can never be silently
      rebound to a different manifest/policy/strategy config.
    * **Admission verdict.** A ``DENY`` from :func:`_build_admission` (missing manifest, reached loss
      cap, breaker, or kill switch) fails closed (``"admission_denied"``).
    """
    if request is not None and (
        request.manifest_hash != manifest.manifest_hash()
        or request.policy_hash != envelope.policy_hash()
        or request.strategy_config_hash != manifest.strategy_config_hash
    ):
        return "manifest_hash_mismatch"
    if admission.verdict == "DENY":
        return "admission_denied"
    return None


def _arming_attempt_ref(arming: ModeBArming) -> str:
    """The non-secret 'current arming attempt' ref bound into the interlock receipt.

    Bound to the REAL per-attempt identity that already exists on the bundle: the signed
    :class:`~veridex.dust_execution.privy_control_plane.PrivyAuthContext`'s ``idempotency_key`` (the
    de-dup key for THIS run's authorized mutation). Read from the LIVE arming bundle on both the
    facade (record) and runner (verify) sides — never invented, never from the proof — so a receipt
    issued for a DIFFERENT authorization attempt does not verify against this bundle. ``""`` when the
    bundle carries no ``order_auth`` (an armed run without it is already blocked upstream).
    """
    return arming.order_auth.idempotency_key if arming.order_auth is not None else ""


def _mode_b_arming_block_reason(
    arming: ModeBArming | None,
    *,
    manifest: StrategyExperimentManifest,
    signer: Signer,
    session_id: str,
    store: OperatorInterlockStore | None,
) -> AbstainReason | None:
    """Return ``"mode_b_not_armed"`` unless EVERY Mode-B arming precondition positively passes.

    Fail-closed AND (default-deny), in a fixed order so the check cannot be partially satisfied:

    #. an absent bundle (no binding at all) → blocked;
    #. the Mode A→B **HARD GATE**: ``mode_a_passed`` MUST be true first;
    #. the E3-T5 CLOB-V2 gate ``mode_b_admitted`` (machine + operator smoke);
    #. the E3-T8 Privy signing preflight ``ok is True``;
    #. the E3-T8 pUSD/approvals/gas provisioning ``ok is True``;
    #. the binding ``binding_hash`` verifies against the pinned manifest field
       (:func:`execute_with`) AND the LIVE policy/quorum verify against the binding
       (:func:`arm_mode_b`) — a :class:`FailClosed` from either (reroute / weakened policy content
       hash / quorum) blocks;
    #. Gate#3 C-1 fix (REQ-016/018): the INJECTED ``signer`` must NOT be the Mode-A
       ``FAKE_LOCAL`` control plane — an armed Mode-B run must consume the REAL keyless Privy/V2
       write path, never the fake local signer used for offline rehearsal
       (``"mode_b_legacy_signer"``);
    #. Gate#3 C-1 fix: the bundle must carry BOTH a non-``None``
       :class:`~veridex.dust_execution.mode_b_write_port.ModeBWritePort` AND a non-``None``
       :class:`~veridex.dust_execution.privy_control_plane.PrivyAuthContext` — an armed run with
       either missing has no real money-moving surface to submit through
       (``"mode_b_write_port_missing"``);
    #. Gate#3 MAJOR-1 (REQ-005): a RECORDED-satisfied :class:`OperatorInterlockProof` must be BOUND
       into the bundle (``satisfied is True`` AND a non-empty ``recording_receipt``) — a
       missing/unsatisfied/unrecorded human interlock returns ``"operator_interlock_unproven"``, so a
       direct (facade-bypassing) runner call with only the technical conditions stays UNARMED.

    The technical branches return closed-vocab ``AbstainReason`` labels distinct enough to diagnose
    which precondition failed, but every one is a REFUSAL (no secret / no live handle ever crosses
    into the reason). Removing ANY branch lets Mode B arm when it must not — the mutation each named
    test trips.
    """
    if arming is None:
        return "mode_b_not_armed"
    if not arming.mode_a_passed:
        # HARD GATE: Mode A (dry-run) must pass before Mode B can arm — even if all else is valid.
        return "mode_b_not_armed"
    if not arming.clobv2_gate.mode_b_admitted:
        return "mode_b_not_armed"
    if arming.privy_preflight.ok is not True:
        return "mode_b_not_armed"
    if arming.provisioning.ok is not True:
        return "mode_b_not_armed"
    try:
        # Binding-hash vs the pinned manifest field (reroute guard), THEN live policy/quorum content
        # hashes vs the binding (weakened-policy / quorum guard). Both fail closed by raising.
        execute_with(manifest, live_binding=arming.binding)
        arm_mode_b(
            binding=arming.binding,
            live_policy=arming.live_policy,
            live_quorum=arming.live_quorum,
        )
    except FailClosed:
        return "mode_b_not_armed"
    # Gate#3 C-1 fix: structural refuse-before-I/O — the Mode-A FAKE_LOCAL signer (or any legacy
    # write client presenting the same closed-vocab mode label) must never reach an ARMED Mode-B
    # run. Checked BEFORE the write-port presence check so a legacy-signer misconfiguration reports
    # its own specific reason.
    if signer.mode == "FAKE_LOCAL":
        return "mode_b_legacy_signer"
    # Gate#3 C-1 fix: an armed run must carry the ONE real money-moving surface (the injected write
    # port) AND its signed authorization context — the generic adapter is a read/cancel/reconcile
    # surface ONLY and is never eligible to substitute for either.
    if arming.write_port is None or arming.order_auth is None:
        return "mode_b_write_port_missing"
    # Gate#3 MAJOR-1 (REQ-005) + M-1: the technical conditions above are necessary but NOT sufficient —
    # the runner is public/exported, so a technical-only or FORGED-receipt bundle must NOT arm real
    # money. The arming artifact must ALSO carry an UNFORGEABLE, STORE-ISSUED human-operator interlock
    # proof: a receipt the injected durable ``store`` ACTUALLY issued for exactly THIS run's session
    # identity, ordered event content, operator authorization, and arming attempt. Presence of a
    # receipt STRING is NOT proof (the M-1 forge) — the store must VERIFY it against its actual rows.
    # Checked LAST so a technical-precondition failure still surfaces its own reason; a missing /
    # unsatisfied / forged / never-issued / wrong-session / altered-event / wrong-attempt proof (or a
    # missing store to verify against) fails closed as ``operator_interlock_unproven``.
    # Gate#3 MAJOR-1: the verdict is DERIVED FROM THE EVENTS, never from the caller-controlled
    # ``proof.satisfied`` bool. Receipt authenticity (``store.verify`` below) only proves "these bytes
    # were stored", NOT "all five human gates passed" — so the runner INDEPENDENTLY re-validates that
    # ``proof.events`` ARE the canonical five REQ-005/006 preconditions (all satisfied + first-order
    # authorized, consistent non-empty operator-auth ref, canonical order) BEFORE any write-port I/O.
    # SEMANTICS first (this run's proof events), THEN AUTHENTICITY (a receipt the store actually issued
    # for THIS run's session/events/auth/attempt); either failing keeps Mode B UNARMED (fail closed).
    proof = arming.operator_interlock
    if proof is None or store is None or not interlock_events_are_canonical(proof.events):
        return "operator_interlock_unproven"
    if not store.verify(
        session_id=session_id,
        events=proof.events,
        operator_authorization_ref=proof.operator_authorization_ref,
        arming_attempt_ref=_arming_attempt_ref(arming),
        receipt=proof.recording_receipt,
    ):
        return "operator_interlock_unproven"
    return None


# ---------------------------------------------------------------------------
# Gate#3 CRITICAL-1: dispatch on the ADMITTED typed intent — never a hardcoded BUY/FOK.
#
# The runner must ACT ON the intent the strategy proposed (and the manifest admitted), not synthesize
# a BUY/FOK taker regardless. ``no_quote`` NEVER submits, ``cancel_all`` fires the sweep (no new
# order), ``cancel_replace`` cancels+replaces, ``make_quote`` rests a post-only maker, ``take`` is a
# taker — each honoring the ADMITTED side/price/TIF. The wire SIZE is still ``resolve_dust_size``
# only (never the agent request), and Mode A still places NO order for ANY intent (AC-017).
# ---------------------------------------------------------------------------

#: The default typed intent for a direct runner call with no agent ``request`` (legacy/self-driven):
#: a BUY/FOK taker, preserving the pre-dispatch behaviour the E6 gate/lifecycle tests pin.
_DEFAULT_INTENT: IntentKind = "take"


def _effective_intent(request: MMExecutionToolRequest | None) -> tuple[IntentKind, MMIntentParams]:
    """Resolve the admitted typed intent + params the runner dispatches on.

    A direct runner call with no ``request`` (self-driven / legacy) defaults to the ``take`` taker
    path with empty params; when a ``request`` is present the runner dispatches on ITS admitted
    ``intent_kind`` / ``intent_params`` — never a hardcoded BUY/FOK synthesized independently.
    """
    if request is None:
        return _DEFAULT_INTENT, MMIntentParams()
    return request.intent_kind, request.intent_params


def _intent_block_reason(
    request: MMExecutionToolRequest | None, *, manifest: StrategyExperimentManifest
) -> AbstainReason | None:
    """Return the abstain reason a NON-order-placing (or non-permitted) intent must fail-closed to.

    Fail-closed, order-stable, applied ONLY when an agent ``request`` is present (a direct
    self-driven call has no agent-proposed intent to gate and takes the default taker path):

    #. an ``intent_kind`` NOT in :attr:`~StrategyExperimentManifest.permitted_intent_kinds` →
       ``"intent_not_permitted"`` (the manifest is the closed set of intents it admits — fail closed);
    #. ``no_quote`` (the explicit DON'T-TRADE abstention) → ``"intent_no_quote"`` (never a submit);
    #. ``cancel_all`` → ``"intent_cancel_all"`` (the sweep fires at session level; no NEW order).

    ``make_quote`` / ``take`` / ``cancel_replace`` return ``None`` here — they place/replace an order
    and are dispatched per-token in :func:`_emit_intent_lifecycle`.
    """
    if request is None:
        return None
    if request.intent_kind not in manifest.permitted_intent_kinds:
        return "intent_not_permitted"
    if request.intent_kind == "no_quote":
        return "intent_no_quote"
    if request.intent_kind == "cancel_all":
        return "intent_cancel_all"
    return None


def _cap_reached(count: int, cap: int) -> bool:
    """Whether an ENABLED order-count ``cap`` is reached by ``count`` (conservative, fail-closed).

    Mirrors the loss-cap boundary predicate (:func:`veridex.policy.envelope.cap_breached`): a cap
    ``<= 0`` is DISABLED (transparent — no ceiling to reach), and an ENABLED cap (``> 0``) is reached
    once ``count >= cap`` — so the moment the count equals the ceiling, one MORE order is denied
    rather than admitted at exactly the maximum. Matches the ``>=`` the E2 policy gate uses for
    ``max_orders_per_run/session/day`` (:func:`veridex.policy.gate.evaluate_pre_quote`).
    """
    return cap > 0 and count >= cap


def _order_cap_block_reason(
    *,
    orders_this_run: int,
    orders_this_session: int,
    orders_this_day: int,
    envelope: PolicyEnvelope,
    manifest: StrategyExperimentManifest,
) -> AbstainReason | None:
    """Return the order-cap abstain reason if placing ONE more order would breach a cap (Gate#3 M-3).

    A PRE-SUBMIT admission gate binding the durable run/session/UTC-day order counts to the SAME
    envelope/manifest cap fields the E2 policy and admission surfaces read, so ``manifest.max_orders``
    is an ENFORCED ceiling — not the metadata it was before. Order of precedence (run → session →
    day) mirrors :func:`veridex.policy.gate.evaluate_pre_quote`. The per-run count is capped by BOTH
    the policy ``max_orders_per_run`` AND the manifest ceiling ``manifest.max_orders`` (either
    reached denies ``order_cap_run``); the session/day counts fold the durable prior counts threaded
    into the runner. Every cap is DISABLED-transparent at ``<= 0`` (:func:`_cap_reached`).

    Reasons reuse the closed-vocab ``order_cap_*`` literals (identical to engine ``_REASON_ORDER_CAP_*``
    / the policy gate) — never a minted synonym.
    """
    if _cap_reached(orders_this_run, envelope.max_orders_per_run) or _cap_reached(
        orders_this_run, manifest.max_orders
    ):
        return "order_cap_run"
    if _cap_reached(orders_this_session, envelope.max_orders_per_session):
        return "order_cap_session"
    if _cap_reached(orders_this_day, envelope.max_orders_per_day):
        return "order_cap_day"
    return None


def _decision_placed(decision: SubmitDecision) -> bool:
    """Whether a decision consumed an order-cap slot — an accepted/possibly-live place, conservatively.

    Increments the order counters ONLY for a decision that ACTUALLY placed (or would place): a Mode B
    submit (``submitted`` — set for an accepted OR possibly-live/uncertain-ACK attempt alike, so an
    unconfirmed attempt still counts), or a Mode A ``mode_a_no_orders`` would-submit (the cap governs
    the DECISION to place, so Mode A rehearses the SAME admission — it touches no wire but consumes a
    slot). Every OTHER abstain (stale, safety_blocked, self_cross, admission_denied, an order_cap_*
    denial itself, …) is a CLEAN abstain that never placed and must NOT consume a slot.
    """
    return decision.submitted or decision.abstain_reason == "mode_a_no_orders"


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


async def run_dust_execution(
    *,
    adapter: VenueAdapter,
    signer: Signer,
    sources: QuoteSource,
    now_fn: Callable[[], int],
    sleep_fn: Callable[[float], Awaitable[None]],
    envelope: PolicyEnvelope,
    manifest: StrategyExperimentManifest,
    mode: ExecutionMode,
    wallet_equity_at_decision: float,
    fixed_fraction: float,
    request: MMExecutionToolRequest | None = None,
    arming: ModeBArming | None = None,
    operator_interlock_store: OperatorInterlockStore | None = None,
    session_identity: str | None = None,
    safety: SafetyController | None = None,
    session: DustSafetySession | None = None,
    risk: RiskAccumulator | None = None,
    breaker: CircuitBreaker | None = None,
    realized_fills: Sequence[RealizedFillRecord] = (),
    own_legs: Sequence[OwnOrderLeg] = (),
    tick_size: float = _DEFAULT_TICK_SIZE,
    shutdown_policy: ShutdownPolicy = "leave_open",
    prior_session_order_count: int = 0,
    prior_day_order_count: int = 0,
) -> DustExecutionResult:
    """Run one dust-execution pass over the manifest universe, applying the submit gates.

    For each token the runner reads the injected source, applies the submit gates, and abstains
    (no order on the wire) on any gate. Only when EVERY gate is clear AND ``mode == "live_guarded"``
    (Mode B) does it build and submit an order; in ``dry_run`` (Mode A) a clean quote still places
    NO order.

    E6-T2: the runner also assembles the full E1-T2 lifecycle-event stream — a session-identity
    preamble (:class:`DustExecutionSessionMeta`) followed by the numbered stream (session-level
    :class:`SessionRiskSnapshot`, then per GATE-CLEAR token: intent -> attempt -> ack -> status ->
    fill/reconciliation, then a session-level :class:`DustRunLabelEvent`). Mode A and Mode B emit
    the IDENTICAL event-type stream for the same input (AC-003); a gate-ABSTAINED token contributes
    no per-decision events (there is no honest order-lifecycle data to record for it).

    E6-T3 (SAF-002/003/009/010) threads the SAFETY WIRING the E6-T2 seams left provisional:

    * **Emergency-stop delegation.** BEFORE the token loop the runner DELEGATES every runner-reachable
      trigger — a realized-loss-cap breach (folded from real fills through the ``RiskAccumulator``), a
      breaker-open, or a kill-switch engage — to the E2-T3 :class:`SafetyController`, which fires the
      venue ``cancel_all_orders`` sweep and BLOCKS submits (:func:`_apply_safety_triggers`). Once
      blocked, every token abstains ``"safety_blocked"`` — no order reaches the wire.
    * **Real risk snapshot.** The ``SessionRiskSnapshot`` now carries the accumulator's REAL
      fee-inclusive loss and the breaker state (not the E6-T2 zero placeholders).
    * **Non-crossing.** Every proposed order routes through the E5
      :func:`~veridex.dust_execution.noncrossing.check_non_crossing` guard before submit; a
      self-crossing order abstains ``"self_cross"`` (SAF-009) — in BOTH modes (mode-independent).
    * **Tri-state reconcile.** After the (Mode B) submit the runner routes the presubmit through the
      E4 :func:`~veridex.dust_execution.reconcile.assess_uncertain_submit` keyed on the
      ``venue_order_key``, so the ``OrderStatusEvent`` / ``RealFillReconciliation`` reflect venue
      truth (RESOLVED/AMBIGUOUS) rather than the E6-T2 hardcoded placeholders.

    E6-T4 (REQ-012/013, AC-004/018/021/024, GUD-001) closes the sizing/pricing + arming seams:

    * **Mechanical size bound to the wire (Codex-M4).** The executable order size comes ONLY from
      :func:`resolve_dust_size` (via :func:`_resolve_wire_size`) over the PINNED
      ``wallet_equity_at_decision`` / ``fixed_fraction`` and the manifest/policy caps — NEVER from the
      agent ``request``'s ``confidence`` / requested ``size`` (they are untrusted metadata with no gate
      or size effect). The E6-T1 ``size=1.0`` placeholder is now real; a Mode-B order's wire price is
      the admitted NATIVE probability (the real V2 order encodes it via ``makerAmount``/``takerAmount``
      — no decimal-odds conversion, Gate#3 C-1 fix).
    * **Manifest authorization (AC-021/024).** A once-per-run deterministic
      :class:`StrategyAuthorizationDecision` (surfaced on the result): a mismatched declared request
      hash fails closed (``"manifest_hash_mismatch"``); an ``EXPERIMENTAL_DUST`` manifest admits
      WITHOUT a profitability flag yet a reached loss cap DENYs (``"admission_denied"``). Identical
      request + hashes → identical admission in dry-run and live-guarded.
    * **Mode A→B HARD GATE + fail-closed arming (AC-004/018).** Mode B builds/submits an order ONLY
      when the ``arming`` bundle passes EVERY precondition (:func:`_mode_b_arming_block_reason`):
      Mode A passed first, the E3-T5 CLOB-V2 gate, the E3-T8 Privy preflight AND pUSD/approvals
      provisioning, and a valid binding whose hash + policy content hash verify. Otherwise every token
      abstains ``"mode_b_not_armed"`` — Mode B stays UNARMED and no order reaches the wire.

    E6-T5 (SAF-005) adds the STARTUP OPEN-ORDER SWEEP before arming: on arm, BEFORE the token loop,
    the runner queries the isolated wallet's PRE-EXISTING open orders (the E3-T2 ``get_orders`` read)
    and, when armed (Mode B), reconciles/cancels them by DELEGATING to the E2-T3
    :meth:`SafetyController.cancel_all_and_block` (:func:`_startup_open_order_sweep`) — it cannot
    blindly submit into pre-existing exposure. A pre-existing resting order fires the ``cancel_all_orders``
    wire AND blocks submits, so every token then abstains ``"safety_blocked"`` — no order lands atop the
    pre-existing orders. Gate#3 MAJOR-2 hardens the READ itself: only a SUCCESSFUL read returning ZERO
    orders permits submit; a FAILED/UNKNOWN read (a reconciliation-capable adapter whose ``get_orders``
    is absent/raises/malformed/incomplete-paginated) is NEVER degraded to "zero open orders" (that
    would be fail-OPEN) — it conservatively sweeps + blocks. Mode A exercises the read but touches no
    wire (dry-run places no orders), and an unavailable read never causes money I/O or a crash.

    E6-T6 (SAF-006, AC-009) closes the shutdown seam: at the END of the run — after the token loop —
    the runner resolves the pinned ``shutdown_policy`` into an EXPLICIT :class:`ShutdownDecision`
    (:func:`_apply_shutdown_decision`), surfaced on the result. Either the cancel-all wire fires (Mode
    B only, via the SAME E2-T3 :meth:`SafetyController.cancel_all_and_block` primitive under the
    ``"shutdown"`` cause) or an explicit ``"leave_open"`` decision is recorded — NEVER a silent
    abandon of resting orders. Mode A records the identical decision contract but never touches the
    wire (AC-017 / AC-003 discipline, mirroring the E6-T5 startup sweep). The returned
    :class:`ShutdownDecision` also distinguishes a FRESH wire fire from an outcome already satisfied
    by an earlier safety sweep (``already_satisfied_by_prior_sweep``, Gate#3 MINOR-1).

    E6-T7 (REQ-014, AC-030, §6 group 13) closes the FINAL E6 seam: the terminal lifecycle
    :class:`SessionOutcome`, surfaced on the result. ``status`` is derived PURELY from the SAFETY
    outcome (:func:`_resolve_session_outcome`) — a bounded (loss caps not breached, breaker not
    open, kill switch not engaged) AND cleanly reconciled (no ``AMBIGUOUS`` :class:`RealFillReconciliation`)
    session is ``"SUCCESS"`` even when it realized a NEGATIVE PnL; a negative dust PnL is the
    EXPECTED, honest outcome of a strategy-neutral safety proof and never flips the status. ``promoted``
    is ALWAYS ``False`` — this lane proves safety, never an alpha claim, so no dust session is ever
    marked as promoted strategy evidence, independent of its ``status``.

    ``sleep_fn`` is the injected async delay seam for the E6 polling loop (added by a later task);
    this pass makes a single deterministic sweep and does not sleep.

    Args:
        adapter: Injected venue adapter (a recording-fake in tests; never a live venue in E6-T1). For
            the safety path it must also expose ``cancel_all_orders`` (a ``CancelAllAdapter``).
        signer: Injected provider-neutral signing control plane (Mode-A fake offline).
        sources: Injected quote source; raises :class:`StaleVenueBook` when gapped/disconnected.
        now_fn: Injected clock returning integer SECONDS (used for the staleness gate and, x1000,
            for every event's integer-millisecond ``recv_ts``).
        sleep_fn: Injected async delay seam (unused in this single-pass skeleton; wired later).
        envelope: Policy envelope providing ``max_quote_age_s``, the loss caps, and ``kill_switch``.
        manifest: Pinned strategy manifest providing the token ``universe`` to quote.
        mode: Execution mode — ``"dry_run"`` (Mode A, no orders) or ``"live_guarded"`` (Mode B).
        wallet_equity_at_decision: PINNED wallet equity at decision time; a mechanical
            :func:`resolve_dust_size` input — never agent-supplied.
        fixed_fraction: PINNED fraction of equity per unit; a mechanical :func:`resolve_dust_size`
            input — never agent-supplied.
        request: Optional typed agent intent. Its declared hashes are cross-checked (fail closed on
            mismatch); its ``confidence`` / ``size`` are untrusted metadata that NEVER reach the wire.
        arming: The Mode-B arming bundle; ``None`` (or any failing precondition) keeps Mode B UNARMED
            (fail closed). Ignored in Mode A (dry-run never arms).
        session_identity: The AUTHORITATIVE, operator-assigned IMMUTABLE session identity the run's
            safety/ledger join binds to (Gate#3 MAJOR-2). The facade threads its durable
            provider's identity here so the ``DustExecutionSessionMeta``, the ``DustSafetySession``,
            the admission, the arming receipt verification, and the default ``RiskAccumulator`` all key
            off the SAME real identity. ``None`` falls back to the provisional ``(strategy_id, mode)``
            derivation (a direct / self-driven runner call).
        safety: The E2-T3 emergency orchestrator the runner delegates every trigger to (a fresh
            :class:`SafetyController` when omitted).
        session: The mutable emergency-stop runtime state (a fresh :class:`DustSafetySession` keyed on
            the session id when omitted).
        risk: The realized-loss accumulator threaded into the risk snapshot and the breach check (a
            fresh one keyed on the session id when omitted).
        breaker: The injected circuit-breaker state; an OPEN breaker delegates to the SafetyController.
        realized_fills: Real venue-reconciled fills folded through the accumulator BEFORE the snapshot;
            a fill that crosses a loss cap fires the emergency sweep.
        own_legs: The runner's own resting legs the non-crossing union is evaluated against.
        tick_size: The venue minimum price increment the non-crossing check uses (E6-T4 binds the real
            per-market tick).
        shutdown_policy: The pinned SAF-006 end-of-run policy — ``"cancel_all"`` fires the E2-T3
            cancel-all wire (Mode B only, and only when THIS call is not already satisfied by a prior
            safety sweep) or ``"leave_open"`` (the default) records an explicit decision to leave
            resting orders open without touching the wire. Always recorded, never omitted.

    Returns:
        A :class:`DustExecutionResult` with one :class:`SubmitDecision` per token, the session
        preamble, the full ordered lifecycle-event stream, the explicit SAF-006
        :class:`ShutdownDecision`, and the terminal REQ-014 :class:`SessionOutcome`.
    """
    seqc = _SeqCounter()
    session_meta = _build_session_meta(
        manifest=manifest,
        envelope=envelope,
        signer=signer,
        mode=mode,
        session_identity=session_identity,
    )

    safety = safety if safety is not None else SafetyController()
    session = session if session is not None else DustSafetySession(session_id=session_meta.session_id)
    risk = risk if risk is not None else RiskAccumulator(session.session_id)

    # DELEGATE every runner-reachable trigger to the SafetyController BEFORE the snapshot, so the
    # snapshot carries the real accumulated loss and a swept session blocks every subsequent submit.
    await _apply_safety_triggers(
        adapter=adapter,
        safety=safety,
        session=session,
        risk=risk,
        envelope=envelope,
        breaker=breaker,
        realized_fills=realized_fills,
    )

    open_order_count = sum(1 for leg in own_legs if leg.kind is LegKind.OPEN)

    # Session-level, mode-independent manifest authorization (computed ONCE, surfaced on the result).
    admission = _build_admission(
        manifest=manifest,
        envelope=envelope,
        risk=risk,
        breaker=breaker,
        session_id=session.session_id,
        open_order_count=open_order_count,
    )
    authorization_block_reason = _authorization_block_reason(
        manifest=manifest, envelope=envelope, request=request, admission=admission
    )

    # Mode A→B HARD GATE + fail-closed arming: computed once; Mode A never arms (reason is None).
    arming_block_reason = (
        _mode_b_arming_block_reason(
            arming,
            manifest=manifest,
            signer=signer,
            session_id=session.session_id,
            store=operator_interlock_store,
        )
        if mode == "live_guarded"
        else None
    )

    # Gate#3 CRITICAL-1: dispatch on the ADMITTED typed intent. ``no_quote`` / ``cancel_all`` / a
    # non-permitted intent block EVERY token from submitting a new order (computed once, mode-
    # independent); ``take`` / ``make_quote`` / ``cancel_replace`` fall through to the per-token
    # dispatch. The wire size stays PINNED-input only; the intent NEVER moves it.
    intent, intent_params = _effective_intent(request)
    intent_block_reason = _intent_block_reason(request, manifest=manifest)

    # Gate#3 C-4: a singular order-placing intent from an AGENT request targets EXACTLY its admitted
    # ``intent_params.token_id`` — enforcement is active only for an agent order-placing intent
    # (``intent_block_reason is None`` rules out no_quote / cancel_all / not_permitted; a self-driven
    # call with no ``request`` fans out per token by design and is NOT token-targeted).
    enforce_intent_token = request is not None and intent_block_reason is None
    intent_target_token = intent_params.token_id if enforce_intent_token else None

    # The mechanical wire size is PINNED-input only (never the agent request) and identical per token.
    wire_size = _resolve_wire_size(
        manifest=manifest,
        envelope=envelope,
        wallet_equity_at_decision=wallet_equity_at_decision,
        fixed_fraction=fixed_fraction,
    )

    events: list[LifecycleEvent] = [
        _build_risk_snapshot(
            seq=seqc.next(),
            now_ms=now_fn() * 1000,
            envelope=envelope,
            risk=risk,
            breaker=breaker,
            open_order_count=open_order_count,
        )
    ]

    # E6-T5 (SAF-005) STARTUP SWEEP: on arm, reconcile/cancel any PRE-EXISTING open orders for the
    # isolated wallet BEFORE the token loop — the runner cannot blindly submit into pre-existing
    # exposure. Enforced (query + cancel-all + block) only when Mode B is ARMED (the "on arm" moment);
    # Mode A / unarmed Mode B exercise the read but touch no wire (they place no order to protect).
    await _startup_open_order_sweep(
        adapter=adapter,
        safety=safety,
        session=session,
        enforce=mode == "live_guarded" and arming_block_reason is None,
    )

    # Gate#3 CRITICAL-1: a ``cancel_all`` intent invokes the E2-T3 cancel-all safety/orchestration
    # path (the SAME single idempotent primitive the safety triggers use) and submits NO new order.
    # Mode B only touches the wire (Mode A places/cancels nothing, AC-017); the token loop then
    # abstains every token ``intent_cancel_all``. Idempotent: if a prior sweep already blocked, this
    # is a no-op that stays blocked.
    if intent_block_reason == "intent_cancel_all" and mode == "live_guarded":
        await safety.cancel_all_and_block(
            "manual", adapter=_require_cancel_all_adapter(adapter), session=session
        )

    # Gate#3 M-3: the PRE-SUBMIT order-count admission. A per-RUN counter increments each time an order
    # is actually placed (Mode B) or would be placed (Mode A rehearsal); the durable prior session/day
    # counts (threaded across a restart/reconstruction) fold into the session/day counts so
    # ``manifest.max_orders`` and the policy run/session/day caps are ENFORCED ceilings — never metadata.
    orders_this_run = 0

    decisions: list[SubmitDecision] = []
    for token_id in manifest.universe:
        order_cap_block_reason = _order_cap_block_reason(
            orders_this_run=orders_this_run,
            orders_this_session=prior_session_order_count + orders_this_run,
            orders_this_day=prior_day_order_count + orders_this_run,
            envelope=envelope,
            manifest=manifest,
        )
        decision, token_events = await _decide_and_submit(
            token_id,
            adapter=adapter,
            signer=signer,
            sources=sources,
            now_fn=now_fn,
            envelope=envelope,
            manifest=manifest,
            mode=mode,
            seqc=seqc,
            safety=safety,
            session=session,
            own_legs=own_legs,
            tick_size=tick_size,
            wire_size=wire_size,
            arming=arming,
            arming_block_reason=arming_block_reason,
            authorization_block_reason=authorization_block_reason,
            order_cap_block_reason=order_cap_block_reason,
            intent=intent,
            intent_params=intent_params,
            intent_block_reason=intent_block_reason,
            enforce_intent_token=enforce_intent_token,
            intent_target_token=intent_target_token,
        )
        decisions.append(decision)
        events.extend(token_events)
        # Increment conservatively AFTER the decision: only an actual/would-be place consumes a slot
        # (a clean abstain — including an order_cap_* denial — never does).
        if _decision_placed(decision):
            orders_this_run += 1

    events.append(_build_label_event(seq=seqc.next(), now_ms=now_fn() * 1000, manifest=manifest))

    # E6-T6 (SAF-006/AC-009): resolve the pinned shutdown policy into an EXPLICIT, recorded decision
    # at the END of the run — cancel-all fires (Mode B only) or an explicit leave-open choice is
    # recorded. Never a silent abandon of resting orders.
    shutdown_decision = await _apply_shutdown_decision(
        adapter=adapter,
        safety=safety,
        session=session,
        mode=mode,
        shutdown_policy=shutdown_policy,
    )

    # E6-T7 (REQ-014/AC-030): the terminal lifecycle STATUS is derived from the SAFETY outcome
    # (bounded loss caps + breaker + kill-switch + clean reconciliation) — NEVER from realized_pnl
    # sign. A bounded, reconciled session is a SUCCESS even if it lost money; it is never promoted.
    session_outcome = _resolve_session_outcome(risk=risk, envelope=envelope, breaker=breaker, events=events)

    return DustExecutionResult(
        mode=mode,
        decisions=tuple(decisions),
        session_meta=session_meta,
        events=tuple(events),
        admission=admission,
        shutdown_decision=shutdown_decision,
        session_outcome=session_outcome,
    )


async def _decide_and_submit(
    token_id: str,
    *,
    adapter: VenueAdapter,
    signer: Signer,
    sources: QuoteSource,
    now_fn: Callable[[], int],
    envelope: PolicyEnvelope,
    manifest: StrategyExperimentManifest,
    mode: ExecutionMode,
    seqc: _SeqCounter,
    safety: SafetyController,
    session: DustSafetySession,
    own_legs: Sequence[OwnOrderLeg],
    tick_size: float,
    wire_size: float,
    arming: ModeBArming | None,
    arming_block_reason: AbstainReason | None,
    authorization_block_reason: AbstainReason | None,
    order_cap_block_reason: AbstainReason | None,
    intent: IntentKind,
    intent_params: MMIntentParams,
    intent_block_reason: AbstainReason | None,
    enforce_intent_token: bool,
    intent_target_token: str | None,
) -> tuple[SubmitDecision, tuple[LifecycleEvent, ...]]:
    """Gate one token's quote and, only when clear AND Mode B (ARMED), act on the ADMITTED intent.

    Also builds the E1-T2 per-decision lifecycle events for a GATE-CLEAR quote — identically shaped
    in both modes (AC-003); see :func:`_emit_order_lifecycle`. A gate-ABSTAINED token (the original
    E6-T1 behavior) emits NO per-decision lifecycle events.

    Gate#3 CRITICAL-1 adds intent dispatch. FIRST — before any I/O — a NON-order-placing / non-
    permitted intent abstains on ``intent_block_reason`` (``intent_no_quote`` / ``intent_cancel_all``
    / ``intent_not_permitted``), so an explicit DON'T-TRADE never submits. When the intent DOES place
    an order (``take`` / ``make_quote`` / ``cancel_replace``) the runner dispatches on it after the
    gates: ``take`` is a taker honoring the admitted side/TIF, ``make_quote`` rests a post-only maker
    at the admitted side/price/TIF, and ``cancel_replace`` cancels the named order then rests its
    replacement — NEVER a hardcoded BUY/FOK.

    E6-T3 adds two refusals AFTER the E6-T1 quote gates and BEFORE any submit: an emergency-stop
    check (once the SafetyController has blocked submits, every token abstains ``"safety_blocked"``)
    and the E5 non-crossing check (a self-crossing proposed order abstains ``"self_cross"``). Both
    refuse with no per-decision lifecycle events, identically in both modes.

    E6-T4 adds the Mode A→B HARD GATE / fail-closed arming — a Mode-B run that is not armed abstains
    ``"mode_b_not_armed"`` BEFORE any quote I/O (refuse-before-I/O) — and, AFTER the safety check
    (which must still win for a swept session), the mode-independent manifest-authorization block
    (``"manifest_hash_mismatch"`` / ``"admission_denied"``). All abstain with no per-decision events.
    The executable size is the PINNED-input ``wire_size`` (:func:`resolve_dust_size`), never the agent
    request.

    Gate#3 M-3 adds the PRE-SUBMIT order-count admission as the LAST gate before the placing dispatch:
    ``order_cap_block_reason`` (a run/session/UTC-day or ``manifest.max_orders`` breach) abstains
    ``"order_cap_run"`` / ``"order_cap_session"`` / ``"order_cap_day"`` mode-independently — the cap
    governs the DECISION to place, so a Mode A would-be (N+1)th decision abstains too (touching no wire).
    It is checked AFTER safety/authorization so a swept/unauthorized run keeps its own honest reason.
    """
    # Gate#3 CRITICAL-1: a non-order-placing / non-permitted intent NEVER submits — abstain on its own
    # honest terms BEFORE any quote I/O (refuse-before-I/O), regardless of arming.
    if intent_block_reason is not None:
        return _abstain(token_id, intent_block_reason), ()

    # Gate#3 C-4: a SINGULAR order-placing intent (make_quote / take / cancel_replace) targets EXACTLY
    # its admitted ``intent_params.token_id``. Every OTHER universe token abstains here — the request
    # authorized ONE token, so it must never fan out and move funds on another. A missing /
    # out-of-universe target matches no loop token, so every token abstains (fail closed, zero wire).
    # Refuse before any quote I/O — a non-target token does no venue read.
    if enforce_intent_token and token_id != intent_target_token:
        return _abstain(token_id, "intent_token_mismatch"), ()

    # Mode A→B HARD GATE + arming (Mode B only): an unarmed Mode B places NO order and reads no quote.
    if mode == "live_guarded" and arming_block_reason is not None:
        return _abstain(token_id, arming_block_reason), ()

    try:
        quote = await sources.read_quote(token_id)
    except StaleVenueBook:
        # A gapped / disconnected / mid-resync source — abstain, nothing reaches the wire.
        return _abstain(token_id, "stale_source"), ()

    now_s = now_fn()
    reason = _evaluate_submit_gate(quote, now_s=now_s, max_quote_age_s=envelope.max_quote_age_s)
    if reason is not None:
        return _abstain(token_id, reason), ()

    # Emergency stop: a swept/blocked session admits NO further submit (SAF-002/003). Checked BEFORE
    # the (also-blocking) admission gate so a loss/breaker/kill sweep keeps its honest ``safety_blocked``.
    if not safety.check_can_submit(session):
        return _abstain(token_id, "safety_blocked"), ()

    # Manifest authorization: a mismatched declared request hash or a DENY admission fails closed
    # (mode-independent — an unauthorized run submits in neither mode).
    if authorization_block_reason is not None:
        return _abstain(token_id, authorization_block_reason), ()

    # Gate#3 M-3: PRE-SUBMIT order-count admission — the LAST gate before the order-placing dispatch.
    # If placing one more order would breach the durable run/session/UTC-day or ``manifest.max_orders``
    # cap, abstain on the closed-vocab ``order_cap_*`` reason (mode-independent: the cap governs the
    # DECISION to place, so it denies identically in Mode A and Mode B). Checked AFTER the (also-
    # blocking) safety and authorization gates so a swept/unauthorized run keeps its own honest reason;
    # a swept run places nothing, so the counter is 0 and this gate stays transparent there.
    if order_cap_block_reason is not None:
        return _abstain(token_id, order_cap_block_reason), ()

    # Gate#3 CRITICAL-1 + C-2: dispatch the ADMITTED order-placing intent (never a hardcoded BUY/FOK),
    # and gate the EXACT proposed typed order — its real token_id/side/native price — through the E5
    # non-crossing check BEFORE any submit (SAF-009). A maker/cancel-replace intent whose typed params
    # are missing/incoherent fails closed with NO wire. Non-crossing is mode-independent (refused in
    # Mode A and Mode B alike).
    if intent == "take":
        side, tif, invalid = _resolve_taker_side_tif(intent_params)
        if invalid:
            return _abstain(token_id, "intent_params_invalid"), ()
        # A taker crosses the book: BUY lifts the ask, SELL hits the bid — the REAL crossing price the
        # order rests at. Both sides are present (a missing side abstained above); safe to read.
        assert quote.bid is not None and quote.ask is not None  # noqa: S101 - gate guaranteed both sides
        proposed = OwnOrderLeg(
            token_id=token_id,
            side=cast("Side", side),  # narrowed to BUY/SELL by _resolve_taker_side_tif (invalid=False)
            price=quote.ask.price if side == "BUY" else quote.bid.price,
            kind=LegKind.PROPOSED,
        )
        cross_reason = _non_crossing_gate(proposed, own_legs=own_legs, tick_size=tick_size)
        if cross_reason is not None:
            return _abstain(token_id, cross_reason), ()
        decision, events = await _emit_order_lifecycle(
            quote,
            adapter=adapter,
            signer=signer,
            envelope=envelope,
            manifest=manifest,
            mode=mode,
            now_s=now_s,
            seqc=seqc,
            wire_size=wire_size,
            tick_size=tick_size,
            arming=arming,
            side=side,
            tif=tif,
        )
    else:
        # ``make_quote`` / ``cancel_replace`` both rest a post-only maker; build+validate it once, then
        # gate the REAL resting order (its admitted side/native price) through non-crossing.
        resting = _build_resting_order(
            token_id=token_id,
            manifest=manifest,
            intent_params=intent_params,
            wire_size=wire_size,
            tick_size=tick_size,
        )
        if resting is None:
            return _abstain(token_id, "intent_params_invalid"), ()
        proposed = OwnOrderLeg(
            token_id=resting.token_id,
            side=resting.side,
            price=resting.native_price,
            kind=LegKind.PROPOSED,
        )
        cross_reason = _non_crossing_gate(proposed, own_legs=own_legs, tick_size=tick_size)
        if cross_reason is not None:
            return _abstain(token_id, cross_reason), ()
        if intent == "cancel_replace":
            replaces = intent_params.replaces_client_order_id
            if not replaces:
                return _abstain(token_id, "intent_params_invalid"), ()
            decision, events = await _emit_cancel_replace_lifecycle(
                quote,
                adapter=adapter,
                signer=signer,
                mode=mode,
                now_s=now_s,
                seqc=seqc,
                resting=resting,
                replaces_client_order_id=replaces,
                tick_size=tick_size,
                arming=arming,
            )
        else:  # make_quote
            decision, events = await _emit_resting_lifecycle(
                quote,
                adapter=adapter,
                signer=signer,
                mode=mode,
                now_s=now_s,
                seqc=seqc,
                resting=resting,
                tick_size=tick_size,
                arming=arming,
            )
    if decision.submitted:
        logger.info(
            "dust_execution.submit",
            extra={"token_id": token_id, "submitted": True, "mode": mode},
        )
    else:
        logger.info(
            "dust_execution.abstain",
            extra={"token_id": token_id, "submitted": False, "abstain_reason": decision.abstain_reason},
        )
    return decision, events


async def _reconcile_after_submit(
    presubmit: PreSubmitRecord, *, adapter: VenueAdapter
) -> tuple[UncertainSubmitState, float]:
    """Reconcile the presubmit against complete venue truth (E4) — the honest tri-state + matched size.

    Routes through the E4 :func:`~veridex.dust_execution.reconcile.assess_uncertain_submit`, keyed on
    the ``venue_order_key``. The adapter is consumed READ-ONLY: ``assess_uncertain_submit`` queries the
    :class:`~veridex.venues.base.VenueReconciliationReads` surfaces defensively (via ``getattr``), so an
    adapter that lacks them degrades fail-closed to AMBIGUOUS with zero matched size — never a fabricated
    fill. The cast reflects that structural, read-only consumption (no reconciliation surface is required
    of every adapter).
    """
    verdict = await assess_uncertain_submit(
        presubmit, adapter=cast("VenueReconciliationReads", adapter)
    )
    return verdict.state, verdict.matched_fill_size


def _status_for(state: UncertainSubmitState, matched_fill_size: float) -> OrderStatus:
    """Map the E4 reconcile verdict to the honest :data:`~veridex.dust_execution.contracts.OrderStatus`.

    AMBIGUOUS (no positive proof, or an unresolved/uncertain submit) is honestly ``"unresolved"``.
    RESOLVED with a matched fill is ``"filled"``; RESOLVED without a matched fill is a terminal that
    left no fill (killed/canceled/expired), reported conservatively as ``"expired"``. Fill size and
    status can never be fabricated — both flow from the reconcile verdict.
    """
    if state == "AMBIGUOUS":
        return "unresolved"
    return "filled" if matched_fill_size > 0.0 else "expired"


def _ack_fields_from_submit_result(submit_result: L2SubmitResult) -> tuple[str | None, bool]:
    """The single source for ``(venue_order_id, accepted)`` from a keyless-transport submit result.

    ``L2SubmitResult.response`` is typed ``dict[str, Any]``, so the venue ACK is read directly with
    no runtime type guard. ``venue_order_id`` is the venue's order id (``None`` when absent) and
    ``accepted`` reflects the venue ``success`` flag, defaulting to whether an order id was returned.
    """
    response = submit_result.response
    order_id = str(response.get("orderID") or response.get("id") or "")
    return (order_id or None), bool(response.get("success", bool(order_id)))


async def _emit_order_lifecycle(
    quote: DustQuote,
    *,
    adapter: VenueAdapter,
    signer: Signer,
    envelope: PolicyEnvelope,
    manifest: StrategyExperimentManifest,
    mode: ExecutionMode,
    now_s: int,
    seqc: _SeqCounter,
    wire_size: float,
    tick_size: float,
    arming: ModeBArming | None,
    side: str = "BUY",
    tif: TimeInForce = "FOK",
) -> tuple[SubmitDecision, tuple[LifecycleEvent, ...]]:
    """Build the full E1-T2 per-decision lifecycle chain for a GATE-CLEAR TAKER quote (AC-003).

    Gate#3 CRITICAL-1: the taker ``side`` / ``tif`` are the ADMITTED intent's (default BUY/FOK for a
    self-driven direct call), NEVER hardcoded — a taker crosses at the ask for a BUY and at the bid
    for a SELL. The wire SIZE is still ``resolve_dust_size`` only (never the agent request).

    Emits, in order: ``OrderSubmitIntent -> OrderSubmitAttempt -> OrderAckEvent ->
    OrderStatusEvent -> RealFillReconciliation``. IDENTICAL event TYPES and ORDERING in both
    modes — Mode A signs via the injected Mode-A fake signer but NEVER submits (the E6-T1
    ``adapter.submit_calls == 0`` / AC-017 invariant is unchanged); its ``OrderAckEvent`` honestly
    records ``ack_status="dry_run_not_submitted"`` and a ``None`` venue_order_id instead of
    fabricating a real acknowledgement.

    E6-T3 closes the status/reconcile seam: the ``OrderStatusEvent`` / ``RealFillReconciliation`` now
    reflect the E4 tri-state reconcile against venue truth (:func:`_reconcile_after_submit`) keyed on
    the ``venue_order_key`` — an adapter with no reconciliation surface degrades fail-closed to
    AMBIGUOUS/``unresolved`` (the honest "no resolved fill" state), identically in both modes.

    E6-T4 binds the REAL executable size: ``wire_size`` (the PINNED-input :func:`resolve_dust_size`
    value — never the agent request's ``confidence`` / requested ``size``) flows into the intent AND
    the compiled Mode-B order identically.

    Gate#3 C-1 fix (REQ-016/018): a Mode-B (``live_guarded``) submit NEVER reaches the generic
    ``adapter.submit_order`` surface or the Mode-A fake signer. The structural guard
    (:func:`_mode_b_arming_block_reason`) already required ``arming`` to be present, armed, and to
    carry a non-``None`` ``write_port`` + ``order_auth`` before this function is ever reached in Mode
    B, so the compiled order is submitted through the injected keyless
    :class:`~veridex.dust_execution.mode_b_write_port.ModeBWritePort`, which returns the REAL compound
    :class:`~veridex.dust_execution.contracts.PreSubmitRecord` (``venue_order_key`` is the official V2
    order hash — never a provisional placeholder). Mode A keeps signing via the injected Mode-A fake
    ``signer.sign_order`` seam (it never submits, so no real venue join key is needed for it).
    """
    assert quote.bid is not None and quote.ask is not None  # noqa: S101 - gate guaranteed both sides

    now_ms = now_s * 1000
    client_order_id = f"{manifest.strategy_id}:{quote.token_id}"
    decision_id = client_order_id
    source_ts = quote.quote_ts_s
    # A taker crosses the book: BUY lifts the ask, SELL hits the bid. Both sides are present here
    # (a missing side would have abstained above), so the native crossing price is safe to read.
    native_price = quote.ask.price if side == "BUY" else quote.bid.price

    intent = OrderSubmitIntent(
        sequence_no=seqc.next(),
        event_type="OrderSubmitIntent",
        source_ts=source_ts,
        recv_ts=now_ms,
        token_id=quote.token_id,
        side=side,
        price=native_price,
        size=wire_size,  # REAL mechanical size — resolve_dust_size(...), never the agent request
        tif=tif,
        client_order_id=client_order_id,
        decision_id=decision_id,
        decision_ts=now_ms,
    )

    submit_result: L2SubmitResult | None = None
    if mode == "live_guarded":
        # The structural guard already required: arming present+armed, write_port+order_auth bound,
        # and signer.mode != "FAKE_LOCAL" — so every value dereferenced below is guaranteed non-None.
        assert arming is not None  # noqa: S101 - guard required arming present+armed before dispatch
        write_port = arming.write_port
        order_auth = arming.order_auth
        assert write_port is not None and order_auth is not None  # noqa: S101 - guard checked this
        submit_result = await write_port.submit_order(
            token_id=quote.token_id,
            side=cast("WireSide", side),  # narrowed to BUY/SELL by _resolve_taker_side_tif
            native_price=native_price,
            size=wire_size,  # REAL mechanical size — resolve_dust_size(...), never the agent request
            tif=tif,
            post_only=False,  # a taker is never post-only (that is the resting-maker lane's field)
            # SINGLE SOURCE: the SAME runner ``tick_size`` the non-crossing gate evaluates against
            # (_non_crossing_gate) — never a second tick source (Gate#3 Stage-2).
            tick_size=tick_size,
            binding=arming.binding,
            auth=order_auth,
        )
        presubmit = submit_result.presubmit_record
    else:
        # Mode A (dry_run): the SAME injected Mode-A fake signer as before — Mode A never submits, so
        # no real venue join key is needed; its own placeholder digest is sufficient for the honest
        # AC-003 event shape.
        payload = SigningPayload(
            token_id=quote.token_id,
            side=side,
            native_price=native_price,
            size=wire_size,
            tif=tif,
            tick_size=f"{tick_size}",
            client_order_id=client_order_id,
        )
        signed = await signer.sign_order(payload)
        presubmit = PreSubmitRecord(
            integrity_commitment_hash=signed.order_digest,
            venue_order_key=f"mode-a-dry-run-digest:{signed.order_digest}",
            captured_id=None,
        )

    attempt = OrderSubmitAttempt(
        sequence_no=seqc.next(),
        event_type="OrderSubmitAttempt",
        source_ts=source_ts,
        recv_ts=now_ms,
        decision_id=decision_id,
        client_order_id=client_order_id,
        request_payload_ref=f"scrubbed://dust-execution/{client_order_id}",
        attempt_ts=now_ms,
        presubmit_record=presubmit,
    )

    venue_order_id: str | None = None
    submitted = False
    if submit_result is not None:
        venue_order_id, accepted = _ack_fields_from_submit_result(submit_result)
        submitted = True
        ack_event: OrderAckEvent = OrderAckEvent(
            sequence_no=seqc.next(),
            event_type="OrderAckEvent",
            source_ts=source_ts,
            recv_ts=now_ms,
            decision_id=decision_id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            ack_status="accepted" if accepted else "not_accepted",
        )
    else:
        # Mode A (dry_run): the SAME typed ack-stage event, honestly recording that NO wire was
        # touched — AC-003 keeps the contract SHAPE identical while never reaching a real submit
        # surface (AC-017 / the E6-T1 invariant).
        ack_event = OrderAckEvent(
            sequence_no=seqc.next(),
            event_type="OrderAckEvent",
            source_ts=source_ts,
            recv_ts=now_ms,
            decision_id=decision_id,
            client_order_id=client_order_id,
            venue_order_id=None,
            ack_status="dry_run_not_submitted",
        )

    # E6-T3: route the presubmit through the E4 tri-state reconcile keyed on the venue_order_key, so
    # status/reconciliation reflect the (recording-fake) venue truth — no longer hardcoded placeholders.
    reconciled_state, matched_fill_size = await _reconcile_after_submit(presubmit, adapter=adapter)
    status_event = OrderStatusEvent(
        sequence_no=seqc.next(),
        event_type="OrderStatusEvent",
        source_ts=source_ts,
        recv_ts=now_ms,
        decision_id=decision_id,
        client_order_id=client_order_id,
        venue_order_id=venue_order_id,
        status=_status_for(reconciled_state, matched_fill_size),
        filled_size=matched_fill_size,
        fill_price=None,
    )
    reconciliation_event = RealFillReconciliation(
        sequence_no=seqc.next(),
        event_type="RealFillReconciliation",
        source_ts=source_ts,
        recv_ts=now_ms,
        decision_id=decision_id,
        venue_order_key=presubmit.venue_order_key,
        reconciled_state=_RECONCILED_STATE[reconciled_state],
        reconciled_fill_size=matched_fill_size,
    )

    decision = SubmitDecision(
        token_id=quote.token_id,
        submitted=submitted,
        abstain_reason=None if submitted else "mode_a_no_orders",
        venue_order_id=venue_order_id,
    )
    events: tuple[LifecycleEvent, ...] = (intent, attempt, ack_event, status_event, reconciliation_event)
    return decision, events


def _resolve_taker_side_tif(intent_params: MMIntentParams) -> tuple[str, TimeInForce, bool]:
    """Resolve a ``take`` intent's ADMITTED side/TIF (default BUY/FOK); flag incoherent params.

    A taker is FAK/FOK only (a GTC/GTD ``tif`` on a taker is incoherent — fail closed); side must be
    ``"BUY"`` / ``"SELL"``. Returns ``(side, tif, invalid)`` — ``invalid`` True means abstain
    ``intent_params_invalid`` (fail-closed, no wire).
    """
    side = intent_params.side if intent_params.side else "BUY"
    tif: TimeInForce = intent_params.tif if intent_params.tif is not None else "FOK"
    invalid = side not in ("BUY", "SELL") or tif not in ("FAK", "FOK")
    return side, tif, invalid


def _build_resting_order(
    *,
    token_id: str,
    manifest: StrategyExperimentManifest,
    intent_params: MMIntentParams,
    wire_size: float,
    tick_size: float,
) -> RestingOrder | None:
    """Build a post-only :class:`RestingOrder` from a ``make_quote`` / ``cancel_replace`` intent.

    Honors the ADMITTED side / native price / TIF (GTC or GTD post-only); the resting SIZE is the
    PINNED-input ``wire_size`` (:func:`resolve_dust_size`), NEVER the agent's requested size. Returns
    ``None`` (fail closed → ``intent_params_invalid``) when a required param is missing or incoherent:
    a non-BUY/SELL side, an absent native price, a non-GTC/GTD tif, or (GTD without an expiration —
    :class:`MMIntentParams` carries no expiration field, so GTD is not wireable here) a
    :class:`RestingOrder` construction that rejects the price/tick/expiration.
    """
    side = intent_params.side
    native_price = intent_params.price
    tif = intent_params.tif
    if side not in ("BUY", "SELL") or native_price is None or tif not in ("GTC", "GTD"):
        return None
    client_order_id = intent_params.client_order_id or f"{manifest.strategy_id}:{token_id}"
    try:
        return RestingOrder(
            token_id=token_id,
            side=side,  # type: ignore[arg-type]  # narrowed to BUY/SELL above
            size=wire_size,  # PINNED mechanical size — resolve_dust_size(...), never the agent request
            native_price=native_price,
            tick_size=tick_size,
            tif=tif,  # narrowed to GTC/GTD above
            post_only=True,
            client_order_id=client_order_id,
        )
    except ValueError:
        # A crossing/untick-aligned price, or GTD with no expiration (unwireable from the intent
        # params): fail closed rather than let a malformed maker reach the wire.
        return None


async def _emit_resting_lifecycle(
    quote: DustQuote,
    *,
    adapter: VenueAdapter,
    signer: Signer,
    mode: ExecutionMode,
    now_s: int,
    seqc: _SeqCounter,
    resting: RestingOrder,
    tick_size: float,
    arming: ModeBArming | None,
) -> tuple[SubmitDecision, tuple[LifecycleEvent, ...]]:
    """Build the per-decision lifecycle for a ``make_quote`` RESTING maker (GTC/GTD post-only).

    Gate#3 C-1 fix (REQ-016/018, AC-031): a Mode-B (``live_guarded``) maker submit NEVER reaches the
    generic ``adapter``/E3-T3 ``submit_resting_order`` write surface — that surface is Mode-A/legacy
    read-only territory now. The structural guard already required ``arming`` to be present, armed,
    and to carry a non-``None`` ``write_port`` + ``order_auth``, so the compiled resting order is
    submitted through the SAME injected keyless
    :class:`~veridex.dust_execution.mode_b_write_port.ModeBWritePort` the taker lane uses — a
    PHYSICALLY DISTINCT resting order type (:class:`RestingOrder` can never represent FAK/FOK) but
    the SAME real money-moving composition (E3-T6 compile -> E3-T8 persist->sign->byte-verify->HMAC).
    Mode A signs via the injected Mode-A fake ``signer.sign_order`` seam and NEVER submits (AC-017).
    Same event TYPES/ORDER as the taker lifecycle.
    """
    now_ms = now_s * 1000
    client_order_id = resting.client_order_id
    decision_id = client_order_id
    source_ts = quote.quote_ts_s

    intent_event = OrderSubmitIntent(
        sequence_no=seqc.next(),
        event_type="OrderSubmitIntent",
        source_ts=source_ts,
        recv_ts=now_ms,
        token_id=resting.token_id,
        side=resting.side,
        price=resting.native_price,
        size=resting.size,  # PINNED mechanical size — resolve_dust_size(...), never the agent request
        tif=resting.tif,
        client_order_id=client_order_id,
        decision_id=decision_id,
        decision_ts=now_ms,
    )

    submit_result: L2SubmitResult | None = None
    if mode == "live_guarded":
        # The structural guard already required: arming present+armed, write_port+order_auth bound,
        # and signer.mode != "FAKE_LOCAL" — so every value dereferenced below is guaranteed non-None.
        assert arming is not None  # noqa: S101 - guard required arming present+armed before dispatch
        write_port = arming.write_port
        order_auth = arming.order_auth
        assert write_port is not None and order_auth is not None  # noqa: S101 - guard checked this
        submit_result = await write_port.submit_order(
            token_id=resting.token_id,
            side=resting.side,  # RestingOrder.side is already Literal["BUY","SELL"] == WireSide
            native_price=resting.native_price,
            size=resting.size,  # PINNED mechanical size — resolve_dust_size(...), never agent-supplied
            tif=resting.tif,
            post_only=resting.post_only,  # the §6 ALO post-only wire field — a REAL maker never crosses
            tick_size=tick_size,
            binding=arming.binding,
            auth=order_auth,
            expiration_s=resting.gtd_expiration_ts or 0,
        )
        presubmit = submit_result.presubmit_record
    else:
        # Mode A (dry_run): the SAME injected Mode-A fake signer as before — Mode A never rests an
        # order, so no real venue join key is needed; its own placeholder digest is sufficient for
        # the honest AC-003 event shape.
        payload = SigningPayload(
            token_id=resting.token_id,
            side=resting.side,
            native_price=resting.native_price,
            size=resting.size,
            tif=resting.tif,
            tick_size=f"{tick_size}",
            client_order_id=client_order_id,
        )
        signed = await signer.sign_order(payload)
        presubmit = PreSubmitRecord(
            integrity_commitment_hash=signed.order_digest,
            venue_order_key=f"mode-a-dry-run-digest:{signed.order_digest}",
            captured_id=None,
        )

    attempt = OrderSubmitAttempt(
        sequence_no=seqc.next(),
        event_type="OrderSubmitAttempt",
        source_ts=source_ts,
        recv_ts=now_ms,
        decision_id=decision_id,
        client_order_id=client_order_id,
        request_payload_ref=f"scrubbed://dust-execution/{client_order_id}",
        attempt_ts=now_ms,
        presubmit_record=presubmit,
    )

    venue_order_id: str | None = None
    submitted = False
    if submit_result is not None:
        venue_order_id, accepted = _ack_fields_from_submit_result(submit_result)
        submitted = True
        ack_event: OrderAckEvent = OrderAckEvent(
            sequence_no=seqc.next(),
            event_type="OrderAckEvent",
            source_ts=source_ts,
            recv_ts=now_ms,
            decision_id=decision_id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            ack_status="accepted" if accepted else "not_accepted",
        )
    else:
        ack_event = OrderAckEvent(
            sequence_no=seqc.next(),
            event_type="OrderAckEvent",
            source_ts=source_ts,
            recv_ts=now_ms,
            decision_id=decision_id,
            client_order_id=client_order_id,
            venue_order_id=None,
            ack_status="dry_run_not_submitted",
        )

    reconciled_state, matched_fill_size = await _reconcile_after_submit(presubmit, adapter=adapter)
    status_event = OrderStatusEvent(
        sequence_no=seqc.next(),
        event_type="OrderStatusEvent",
        source_ts=source_ts,
        recv_ts=now_ms,
        decision_id=decision_id,
        client_order_id=client_order_id,
        venue_order_id=venue_order_id,
        status=_status_for(reconciled_state, matched_fill_size),
        filled_size=matched_fill_size,
        fill_price=None,
    )
    reconciliation_event = RealFillReconciliation(
        sequence_no=seqc.next(),
        event_type="RealFillReconciliation",
        source_ts=source_ts,
        recv_ts=now_ms,
        decision_id=decision_id,
        venue_order_key=presubmit.venue_order_key,
        reconciled_state=_RECONCILED_STATE[reconciled_state],
        reconciled_fill_size=matched_fill_size,
    )

    decision = SubmitDecision(
        token_id=resting.token_id,
        submitted=submitted,
        abstain_reason=None if submitted else "mode_a_no_orders",
        venue_order_id=venue_order_id,
    )
    events: tuple[LifecycleEvent, ...] = (
        intent_event,
        attempt,
        ack_event,
        status_event,
        reconciliation_event,
    )
    return decision, events


async def _old_order_terminal_withdrawn(order_id: str, *, adapter: VenueAdapter) -> bool:
    """``True`` iff COMPLETE venue truth proves the NAMED order terminal-WITHDRAWN (gone AND not filled).

    REQ-009: a single-order cancel ACK is NON-TERMINAL — the possibly-live old order stays exposure
    until open-order/status RECONCILIATION establishes withdrawal. This reconciles the named order
    through the SAME E4 three-surface truth the submit path uses (:func:`_reconcile_after_submit` →
    :func:`~veridex.dust_execution.reconcile.assess_uncertain_submit`: ``get_orders`` ∪
    ``get_order``-by-id ∪ ``get_fill_history``, keyed by the official order id) — no new truth read is
    invented.

    Mirrors the E4 tri-state semantics WITHOUT fabricating ``DEFINITIVELY_ABSENT`` (which E4-T6 proves
    is unreachable via the real venue surfaces): the old order is WITHDRAWN only on a ``RESOLVED``
    verdict carrying NO matching fill — positive terminal evidence of a NON-FILL terminal status
    (canceled / killed / expired), i.e. gone AND not filled. Every other outcome is possibly-live and
    fails closed to NOT-withdrawn:

    * ``AMBIGUOUS`` — still resting, a bare-zero open-order read (never proof of absence), or an
      unavailable/raising reconcile surface — the old order may still be live.
    * ``RESOLVED`` WITH a matched fill — the old order FILLED; it was not withdrawn.
    """
    presubmit = PreSubmitRecord(
        integrity_commitment_hash="",  # no private digest for a pre-existing order; reconcile keys on the id
        venue_order_key=order_id,
        captured_id=None,
    )
    state, matched_fill_size = await _reconcile_after_submit(presubmit, adapter=adapter)
    return state == "RESOLVED" and matched_fill_size <= 0.0


async def _emit_cancel_replace_lifecycle(
    quote: DustQuote,
    *,
    adapter: VenueAdapter,
    signer: Signer,
    mode: ExecutionMode,
    now_s: int,
    seqc: _SeqCounter,
    resting: RestingOrder,
    replaces_client_order_id: str,
    tick_size: float,
    arming: ModeBArming | None,
) -> tuple[SubmitDecision, tuple[LifecycleEvent, ...]]:
    """Honest cancel-replace: cancel the NAMED order (E3-T4 ``DELETE /order``), then rest the replacement
    ONLY after complete venue truth proves the old order terminal-WITHDRAWN.

    NOT a blind BUY/FOK: it first cancels EXACTLY the named order via the single-order cancel wire
    (fail closed if the adapter cannot) and records an
    :class:`~veridex.dust_execution.contracts.OrderCancelEvent` (honest ``canceled`` flag; a phantom
    cancel is never reported as success). REQ-009: the cancel ACK is NON-TERMINAL, so the replacement is
    rested through :func:`_emit_resting_lifecycle` ONLY once :func:`_old_order_terminal_withdrawn`
    reconciles the named order to a terminal-WITHDRAWN state (gone AND not filled). A failed / still-
    resting / ambiguous / filled cancel places ZERO replacement wire calls — resting a replacement atop
    a possibly-live old order would create DOUBLE exposure (old + new both live) — and abstains honestly.
    Mode A touches NO wire (AC-017): it never reconciles-to-abstain here, so both modes still emit the
    identical cancel + resting lifecycle shape.
    """
    now_ms = now_s * 1000
    source_ts = quote.quote_ts_s
    canceled = False
    old_order_withdrawn = False
    if mode == "live_guarded":
        cancel_client = _require_single_cancel_venue(adapter)
        response = await cancel_client.cancel_single_order(replaces_client_order_id)
        canceled_ids = response.get("canceled") if isinstance(response, dict) else None
        canceled = isinstance(canceled_ids, list) and replaces_client_order_id in canceled_ids
        # REQ-009: the ACK is non-terminal — the old order stays exposure until venue truth proves it
        # withdrawn. Reconcile the NAMED order BEFORE resting any replacement.
        old_order_withdrawn = await _old_order_terminal_withdrawn(
            replaces_client_order_id, adapter=adapter
        )
    cancel_event = OrderCancelEvent(
        sequence_no=seqc.next(),
        event_type="OrderCancelEvent",
        source_ts=source_ts,
        recv_ts=now_ms,
        decision_id=resting.client_order_id,
        client_order_id=resting.client_order_id,
        venue_order_id=None,
        canceled=canceled,
    )
    if mode == "live_guarded" and not old_order_withdrawn:
        # The named old order is NOT proven terminal-withdrawn (failed / still-resting / ambiguous /
        # filled cancel): resting a replacement now risks DOUBLE exposure. Place NOTHING on the resting
        # wire and abstain honestly (REQ-009) — the possibly-live old order remains the only exposure.
        return _abstain(resting.token_id, "cancel_replace_old_order_live"), (cancel_event,)
    decision, resting_events = await _emit_resting_lifecycle(
        quote,
        adapter=adapter,
        signer=signer,
        mode=mode,
        now_s=now_s,
        seqc=seqc,
        resting=resting,
        tick_size=tick_size,
        arming=arming,
    )
    return decision, (cancel_event, *resting_events)


def _abstain(token_id: str, reason: AbstainReason) -> SubmitDecision:
    """Build an abstaining decision (no order on the wire) with boolean/id-only telemetry."""
    logger.info(
        "dust_execution.abstain",
        extra={"token_id": token_id, "submitted": False, "abstain_reason": reason},
    )
    return SubmitDecision(token_id=token_id, submitted=False, abstain_reason=reason, venue_order_id=None)


__all__ = [
    "ABSTAIN_REASONS",
    "AbstainReason",
    "BookSide",
    "DustExecutionResult",
    "DustQuote",
    "LifecycleEvent",
    "ModeBArming",
    "OperatorInterlockProof",
    "QuoteSource",
    "SessionOutcome",
    "SessionStatus",
    "ShutdownDecision",
    "ShutdownPolicy",
    "StaleVenueBook",
    "SubmitDecision",
    "provisional_session_id",
    "run_dust_execution",
]
