"""R4-A agent-callable MM tool boundary contracts (Section 4.3, AC-020, §6 group 10).

The two frozen models here are the ONLY typed surface between the R4-B strategy/agent layer and
the policy-gated dust-execution runner:

* :class:`MMExecutionToolRequest` — a strategy PROPOSES a typed intent (``make_quote`` / ``take``
  / ``cancel_replace`` / ``cancel_all`` / ``no_quote``) together with the hashes it DECLARES it
  was admitted under. The sanctioned admission constructor :meth:`MMExecutionToolRequest.build`
  cross-checks those declared hashes against the ADMITTED pins and **fails closed** (raises) on
  any mismatch, so an approved intent can never be silently re-bound to a different
  manifest/policy/strategy config (§4.3). A missing pinned hash is rejected at construction
  (``extra="forbid"`` + required field).
* :class:`MMExecutionToolResult` — the boundary returns ONLY a typed ``admission`` verdict, ordered
  ``reason_codes``, an OPAQUE ``lifecycle_receipt_ref`` string, the honest labels, and the
  ``policy_hash``. It NEVER carries a raw venue client, signer, wallet, or private-key handle
  (AC-020): every field bottoms out in a JSON-primitive or a pinned ``Literal``.

The CONTRACTS import ONLY ``.contracts`` (same isolated package — SEC-003 permits the intra-lane
import of the frozen base + pinned labels) and the standard library; they do NOT import
``veridex.live_recorder`` and carry NO ranked-lane dependency.

E7-T1 adds the injectable :func:`propose_mm_execution` proposer/adapter (REQ-003, AC-019/020/026):
it takes an :class:`MMExecutionToolRequest`, drives R4-A admission/execution/reconciliation THROUGH
:func:`veridex.dust_execution.runner.run_dust_execution` (imported LAZILY inside the call to break
the runner<->facade import cycle), and returns a typed :class:`MMExecutionToolResult` + an OPAQUE
lifecycle receipt REF — never a live venue/signer/client handle (AC-020). Its lifecycle emits ONLY
into the OPS :class:`~veridex.runtime.runtime_events.RuntimeEvent` sink — NEVER by registering a
tool on the ``tools=[]`` decision agent (``veridex.runtime.agent``, whose empty tools list is a HARD
invariant). R4-A ships safety-complete WITHOUT R4-B: the proposer functions with a pinned
``EXPERIMENTAL_DUST`` manifest alone and requires no real/promoted strategy or alpha to run.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import field_validator

from veridex.dust_execution.contracts import (
    OPERATOR_PRECONDITIONS,
    PRECONDITION_FIRST_ORDER_AUTHORIZED,
    PRECONDITION_ISOLATED_FUNDED_WALLET,
    PRECONDITION_JURISDICTION_COMFORT,
    PRECONDITION_KILL_SWITCH_READY,
    PRECONDITION_MAX_CAPITAL_AT_RISK,
    DustRunLabelEvent,
    EvidenceClass,
    ExecutionMode,
    OperatorInterlockEvent,
    OrderAckEvent,
    OrderSubmitAttempt,
    RealFillReconciliation,
    TimeInForce,
    _FrozenModel,
    _reject_price_out_of_unit_interval,
)
from veridex.dust_execution.operator_interlock_store import (
    OperatorInterlockStore,
    interlock_events_are_canonical,
)
from veridex.runtime.runtime_events import RuntimeEventType, RuntimeStatus, runtime_event

if TYPE_CHECKING:
    from veridex.dust_execution.manifest import StrategyExperimentManifest
    from veridex.dust_execution.runner import DustExecutionResult, ModeBArming, QuoteSource
    from veridex.dust_execution.session_state import (
        DurableSessionStateProvider,
        ReconciliationState,
    )
    from veridex.dust_execution.signer import Signer
    from veridex.policy.envelope import PolicyEnvelope
    from veridex.runtime.runtime_events import RuntimeEventSink
    from veridex.venues.base import VenueAdapter

# The closed set of agent-proposable intent kinds (§4.3). ``no_quote`` is an explicit abstention.
IntentKind = Literal["make_quote", "take", "cancel_replace", "cancel_all", "no_quote"]

# The typed admission verdict returned to the agent (§4.3): approved, denied, or human-gated.
# NOTE: ``admission`` reports ONLY whether the STRATEGY MANIFEST was admitted — NOT whether an order
# executed. A withheld/abstained execution can carry ``admission="APPROVED"`` (Gate#3 MAJOR-3), so a
# consumer MUST read ``execution_status`` (below) to learn what actually happened on the wire.
Admission = Literal["APPROVED", "DENIED", "REQUIRES_HUMAN"]

# The closed vocabulary of EXECUTION dispositions (Gate#3 MAJOR-3) — what ACTUALLY happened on the
# wire, DERIVED from the runner's real ``DustExecutionResult`` (submits/abstains), never re-derived
# from the strategy ``admission``. This is deliberately SEPARATE from ``Admission`` so a Studio /
# AgentRuntime consumer can read "strategy admitted" and "execution withheld/abstained" INDEPENDENTLY:
#   * ``SUBMITTED``  — at least one order actually reached the wire;
#   * ``ABSTAINED``  — the gates/intent abstained (e.g. ``intent_no_quote`` / ``stale_quote_age`` /
#                      ``safety_blocked``) — the strategy was admitted but no order was placed;
#   * ``NOT_ARMED``  — Mode B could not arm (``mode_b_not_armed`` / ``operator_interlock_unproven``),
#                      so execution was WITHHELD — a withheld execution NEVER reads as SUBMITTED;
#   * ``DENIED``     — the strategy admission itself was DENIED, so no execution was authorized.
ExecutionStatus = Literal["SUBMITTED", "ABSTAINED", "NOT_ARMED", "DENIED"]

# The abstain reasons that mean "Mode B could not ARM" (execution WITHHELD for want of arming/interlock
# proof), as opposed to a strategy/gate/intent abstention. Mirrors the runner's ``AbstainReason``
# closed vocab; membership drives the ``NOT_ARMED`` disposition. A closed set — never an id or secret.
_NOT_ARMED_ABSTAIN_REASONS: frozenset[str] = frozenset(
    {"mode_b_not_armed", "operator_interlock_unproven"}
)


class MMIntentParams(_FrozenModel):
    """Typed parameters for a proposed MM intent (§4.3 ``intent_params``).

    Deliberately typed (never ``dict[str, Any]``): every field is a primitive with a native
    ``[0,1]`` price guard (CON-004), so a malformed/odds-style intent is rejected at construction.
    All fields are optional because their applicability depends on ``intent_kind`` (e.g.
    ``cancel_all`` / ``no_quote`` carry none, ``cancel_replace`` names the order it replaces via
    ``replaces_client_order_id``). ``extra="forbid"`` (inherited) still rejects any leaked field.
    """

    token_id: str | None = None
    side: str | None = None
    price: float | None = None
    size: float | None = None
    tif: TimeInForce | None = None
    client_order_id: str | None = None
    replaces_client_order_id: str | None = None

    @field_validator("price")
    @classmethod
    def _price_in_unit_interval(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _reject_price_out_of_unit_interval(value)


class MMExecutionToolRequest(_FrozenModel):
    """A typed, frozen agent-callable MM execution request (§4.3).

    Carries the pinned admission hashes the strategy DECLARES it is operating under
    (``strategy_config_hash`` / ``policy_hash`` / ``manifest_hash``) plus a typed intent. Every
    pinned hash is a REQUIRED field, so a missing one is rejected at construction
    (``extra="forbid"``). ``reason`` / ``confidence`` are OPTIONAL untrusted agent metadata with
    NO gate effect (AC-022) — they never move admission.

    Use :meth:`build` as the admission entry point: it fails closed on a hash mismatch. Direct
    construction is a plain data carrier of the strategy's declaration and does NOT (cannot) know
    the admitted pins — the cross-check lives in :meth:`build`.
    """

    intent_kind: IntentKind
    intent_params: MMIntentParams
    strategy_id: str
    strategy_config_hash: str
    policy_hash: str
    session_id: str
    manifest_hash: str
    evidence_class: EvidenceClass
    mode: ExecutionMode
    reason: str | None = None  # untrusted agent metadata; no gate effect (AC-022)
    confidence: float | None = None  # untrusted agent metadata; no gate effect (AC-022)

    @classmethod
    def build(
        cls,
        *,
        intent_kind: IntentKind,
        intent_params: MMIntentParams,
        strategy_id: str,
        strategy_config_hash: str,
        policy_hash: str,
        session_id: str,
        manifest_hash: str,
        evidence_class: EvidenceClass,
        mode: ExecutionMode,
        admitted_manifest_hash: str,
        admitted_policy_hash: str,
        admitted_strategy_config_hash: str,
        reason: str | None = None,
        confidence: float | None = None,
    ) -> MMExecutionToolRequest:
        """Construct a request only if the declared pins MATCH the admitted pins (fail closed).

        The strategy declares ``manifest_hash`` / ``policy_hash`` / ``strategy_config_hash``; this
        constructor compares each against the corresponding ADMITTED pin and RAISES
        :class:`ValueError` on any mismatch, so an approved intent can never be silently rerouted
        to a different manifest/policy/strategy config (§4.3, group 12). Mismatches are reported in
        a fixed order for a deterministic message.
        """
        mismatches: list[str] = []
        if manifest_hash != admitted_manifest_hash:
            mismatches.append("manifest_hash")
        if policy_hash != admitted_policy_hash:
            mismatches.append("policy_hash")
        if strategy_config_hash != admitted_strategy_config_hash:
            mismatches.append("strategy_config_hash")
        if mismatches:
            raise ValueError(
                "MM execution request fails closed: declared hashes do not match the admitted "
                f"pins for {', '.join(mismatches)}"
            )
        return cls(
            intent_kind=intent_kind,
            intent_params=intent_params,
            strategy_id=strategy_id,
            strategy_config_hash=strategy_config_hash,
            policy_hash=policy_hash,
            session_id=session_id,
            manifest_hash=manifest_hash,
            evidence_class=evidence_class,
            mode=mode,
            reason=reason,
            confidence=confidence,
        )


class MMExecutionToolResult(_FrozenModel):
    """The typed, frozen result returned across the agent boundary (§4.3, AC-020).

    Carries ONLY: a typed ``admission`` verdict, ordered ``reason_codes``, an OPAQUE
    ``lifecycle_receipt_ref`` (a string reference into the lifecycle evidence, never a live
    object), the honest labels, and ``policy_hash``. It NEVER carries a raw venue client, signer,
    wallet, or private-key handle — every field is a JSON-primitive or a pinned ``Literal``, which
    makes the no-raw-handle guarantee STRUCTURAL (§6 group 10).

    The honest labels reuse the pinned literals from ``contracts.DustRunLabelEvent`` so a dust run
    can never be relabeled as validated/promoted (AC-025); there is deliberately NO
    ``expected_pnl`` / ``edge_bps`` field — the result implies no profitability/edge claim.

    Gate#3 MAJOR-3: ``admission`` reports ONLY the STRATEGY-MANIFEST verdict ("the strategy was
    admitted"), NEVER "an order executed". The SEPARATE closed-vocab ``execution_status`` (+ its
    ``execution_reason_codes``) reports what ACTUALLY happened on the wire — ``SUBMITTED`` /
    ``ABSTAINED`` / ``NOT_ARMED`` / ``DENIED`` — DERIVED from the runner's real disposition, so a
    WITHHELD execution (interlock/arming) can never read as an executed approval. The two fields are
    deliberately distinct: a consumer reads "strategy admitted" and "execution withheld" separately.
    ``execution_reason_codes`` is a closed-vocab list of the runner's abstain reasons (SEC-005 — never
    a fill/PnL/rankable value).
    """

    admission: Admission
    reason_codes: tuple[str, ...]
    # FAIL-CLOSED defaults (Gate#3 MAJOR-3): an unspecified execution disposition defaults to
    # ``ABSTAINED`` ("no order reached the wire") — NEVER ``SUBMITTED`` — so a hand-constructed result
    # can never falsely imply an execution. The production mapping (:func:`_to_tool_result`) ALWAYS
    # sets both explicitly from the runner's REAL disposition; the defaults only spare direct
    # constructors (e.g. offline test doubles) from a wrongly-optimistic execution claim.
    execution_status: ExecutionStatus = "ABSTAINED"
    execution_reason_codes: tuple[str, ...] = ()
    lifecycle_receipt_ref: str
    run_label: Literal["DUST_LIVE"]  # pinned, mirrors contracts.DustRunLabelEvent.run_label
    calibration_label: Literal["UNCALIBRATED"]  # mirrors DustRunLabelEvent.calibration_label
    edge_label: Literal["NOT_PROVEN_EDGE"]  # mirrors DustRunLabelEvent.edge_label
    evidence_class: EvidenceClass
    policy_hash: str


# ---------------------------------------------------------------------------
# E7-T1 — the injectable MM facade proposer (R4-B intent -> R4-A execute).
# ---------------------------------------------------------------------------

#: Default OPS ``agent_id`` for facade-emitted lifecycle telemetry (a non-secret label).
_FACADE_AGENT_ID = "dust-execution-mm"

#: Pinned honest labels — the fallback if a run somehow emits no terminal label event (defensive;
#: the runner always appends one). Each mirrors ``contracts.DustRunLabelEvent`` so a dust run can
#: never be relabeled as validated/promoted (AC-025).
_DEFAULT_RUN_LABEL: Literal["DUST_LIVE"] = "DUST_LIVE"
_DEFAULT_CALIBRATION_LABEL: Literal["UNCALIBRATED"] = "UNCALIBRATED"
_DEFAULT_EDGE_LABEL: Literal["NOT_PROVEN_EDGE"] = "NOT_PROVEN_EDGE"
#: The honest evidence class a dust run defaults to — PINNED like its sibling labels, never taken
#: from the (untrusted) agent request even on the unreachable fallback path (AC-025 consistency).
_DEFAULT_EVIDENCE_CLASS: Literal["EXPERIMENTAL_DUST"] = "EXPERIMENTAL_DUST"


def _lifecycle_receipt_ref(result: DustExecutionResult) -> str:
    """Derive an OPAQUE, deterministic reference into the run's lifecycle evidence stream.

    The ref pins the session identity + the numbered ``sequence_no`` stream via a sha256 digest, so
    an operator can locate the sealed lifecycle evidence WITHOUT the boundary ever handing back a
    live object. It is a REFERENCE STRING only — never a venue client, signer, wallet, or key
    (AC-020). Identical inputs → identical ref (byte-stable).
    """
    meta = result.session_meta
    canonical = json.dumps(
        {
            "session_id": meta.session_id,
            "mode": result.mode,
            "manifest_hash": meta.manifest_hash,
            "policy_hash": meta.policy_hash,
            "content_hash": meta.content_hash,
            "sequence_nos": [event.sequence_no for event in result.events],
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"dust-lifecycle:{meta.session_id}:{digest[:16]}"


def _terminal_label(result: DustExecutionResult) -> DustRunLabelEvent | None:
    """Return the terminal :class:`DustRunLabelEvent` the run emitted (the honest-labels source)."""
    for event in reversed(result.events):
        if isinstance(event, DustRunLabelEvent):
            return event
    return None


def _execution_disposition(
    result: DustExecutionResult, admission: Admission
) -> tuple[ExecutionStatus, tuple[str, ...]]:
    """Derive the EXECUTION disposition from the runner's REAL run (Gate#3 MAJOR-3), never admission.

    Reports what ACTUALLY happened on the wire, threaded from the runner's own
    :class:`~veridex.dust_execution.runner.DustExecutionResult` disposition — its per-token
    :class:`~veridex.dust_execution.runner.SubmitDecision` ``submitted``/``abstain_reason`` and the
    ``submitted_count`` — so a withheld/abstained execution is NEVER re-derived from (and can never be
    masked by) the strategy ``admission``:

      * a DENIED strategy admission authorizes NO execution → ``DENIED``;
      * else, at least one order reaching the wire → ``SUBMITTED`` (submission is the strongest, most
        honest disposition even if other tokens abstained);
      * else, an abstain caused by Mode B being unable to ARM (``mode_b_not_armed`` /
        ``operator_interlock_unproven``) → ``NOT_ARMED`` (execution WITHHELD);
      * else, any other gate/intent abstain → ``ABSTAINED``.

    ``execution_reason_codes`` is the DISTINCT set of the runner's abstain reasons in first-seen order
    (a closed-vocab, id-free, non-rankable list — SEC-005). It is empty when every decision submitted.
    """
    reason_codes = tuple(
        dict.fromkeys(
            decision.abstain_reason
            for decision in result.decisions
            if decision.abstain_reason is not None
        )
    )
    if admission == "DENIED":
        return "DENIED", reason_codes
    if result.submitted_count > 0:
        return "SUBMITTED", reason_codes
    if any(reason in _NOT_ARMED_ABSTAIN_REASONS for reason in reason_codes):
        return "NOT_ARMED", reason_codes
    return "ABSTAINED", reason_codes


def _to_tool_result(
    result: DustExecutionResult, request: MMExecutionToolRequest
) -> MMExecutionToolResult:
    """Map the runner's :class:`DustExecutionResult` onto the typed boundary result (AC-020).

    Carries ONLY the STRATEGY admission verdict, ordered reason codes, the SEPARATE EXECUTION
    disposition (``execution_status`` + ``execution_reason_codes`` — Gate#3 MAJOR-3), an OPAQUE
    lifecycle receipt REF, the honest labels the run actually emitted, and the admitted ``policy_hash``
    — NEVER the runner result object, adapter, signer, or any live handle. An ``ALLOW`` admission maps
    to ``APPROVED``, any ``DENY`` to ``DENIED``; the ``execution_status`` is derived SEPARATELY from
    the runner's real run disposition so a withheld execution never reads as an executed approval.
    """
    admission: Admission = "APPROVED" if result.admission.verdict == "ALLOW" else "DENIED"
    execution_status, execution_reason_codes = _execution_disposition(result, admission)
    label = _terminal_label(result)
    return MMExecutionToolResult(
        admission=admission,
        reason_codes=result.admission.reason_codes,
        execution_status=execution_status,
        execution_reason_codes=execution_reason_codes,
        lifecycle_receipt_ref=_lifecycle_receipt_ref(result),
        run_label=label.run_label if label is not None else _DEFAULT_RUN_LABEL,
        calibration_label=(
            label.calibration_label if label is not None else _DEFAULT_CALIBRATION_LABEL
        ),
        edge_label=label.edge_label if label is not None else _DEFAULT_EDGE_LABEL,
        evidence_class=label.evidence_class if label is not None else _DEFAULT_EVIDENCE_CLASS,
        policy_hash=result.admission.policy_hash,
    )


# ---------------------------------------------------------------------------
# E7-T3 — the human operator precondition interlock that gates Mode-B arming.
#
# Mode B (real money) cannot ARM unless ALL FIVE human operator preconditions are POSITIVELY
# satisfied AND recorded via ``OperatorInterlockEvent`` (REQ-005/006, AC-002). A MISSING precondition
# is an explicit no-go: the facade WITHHOLDS the Mode-B arming bundle (feeds ``arming=None`` into the
# EXISTING E6-T4 ``_mode_b_arming_block_reason`` gate), so Mode B stays UNARMED — the SAME fail-closed
# ``"mode_b_not_armed"`` outcome, never a parallel arming path.
# ---------------------------------------------------------------------------

#: The five human operator preconditions + their fixed recording order are the closed vocabulary shared
#: lane-wide; they now live in ``contracts`` (the lowest module) so the store validator, the runner,
#: and this facade agree on ONE canonical set. They are IMPORTED above (not redefined here) and used by
#: :meth:`OperatorInterlock.satisfied_by_precondition` / :func:`evaluate_operator_interlock` below; the
#: import keeps ``from veridex.dust_execution.facade import OPERATOR_PRECONDITIONS`` working (back-compat).


class OperatorInterlock(_FrozenModel):
    """The five human operator preconditions that gate Mode-B arming (REQ-005/006, AC-002).

    Every field is an OPERATOR-supplied assertion: the interlock RECORDS it and concludes NOTHING on
    the operator's behalf — most importantly ``operator_jurisdiction_comfort`` is the operator's OWN
    legal-comfort assertion, which the model only records (it makes NO jurisdiction/legal
    conclusion). Each field defaults to the fail-closed value (unset = NOT satisfied), so a
    partially-filled or defaulted interlock is a no-go — Mode B cannot arm on omission.
    """

    isolated_funded_wallet: bool = False
    operator_jurisdiction_comfort: bool = False
    declared_max_capital_at_risk: float | None = None
    kill_switch_ready: bool = False
    first_order_authorized: bool = False
    operator_authorization_ref: str | None = None

    def satisfied_by_precondition(self) -> dict[str, bool]:
        """Map each precondition id -> whether the OPERATOR positively satisfied it (fail closed).

        ``declared_max_capital_at_risk`` is satisfied ONLY when a POSITIVE magnitude is declared
        (``None`` or ``<= 0`` is a no-go); every other precondition is a strict ``is True`` (never a
        truthy coercion). This is a pure MIRROR of the operator's supplied assertions — it derives
        no precondition, and in particular reaches NO jurisdiction/legal conclusion.
        """
        capital = self.declared_max_capital_at_risk
        return {
            PRECONDITION_ISOLATED_FUNDED_WALLET: self.isolated_funded_wallet is True,
            PRECONDITION_JURISDICTION_COMFORT: self.operator_jurisdiction_comfort is True,
            PRECONDITION_MAX_CAPITAL_AT_RISK: capital is not None and capital > 0.0,
            PRECONDITION_KILL_SWITCH_READY: self.kill_switch_ready is True,
            PRECONDITION_FIRST_ORDER_AUTHORIZED: self.first_order_authorized is True,
        }


@dataclass(frozen=True)
class OperatorInterlockGate:
    """The recorded outcome of evaluating the human operator interlock (the REQ-005 audit trail).

    ``armed`` is True ONLY when EVERY precondition is satisfied; ``missing`` names (in the fixed
    :data:`OPERATOR_PRECONDITIONS` order) each unsatisfied precondition; ``events`` is one recorded
    :class:`~veridex.dust_execution.contracts.OperatorInterlockEvent` per precondition (``satisfied``
    True/False). Carries ONLY bool / closed-vocab / non-secret-ref data (SEC-005).
    """

    armed: bool
    missing: tuple[str, ...]
    events: tuple[OperatorInterlockEvent, ...]


def evaluate_operator_interlock(
    interlock: OperatorInterlock | None, *, recv_ts_ms: int
) -> OperatorInterlockGate:
    """Evaluate + RECORD the human operator interlock; ``armed`` iff ALL FIVE preconditions hold.

    Fail-closed: a ``None`` interlock (none supplied) is treated as an all-default (all-unsatisfied)
    interlock — Mode B cannot arm without a positively-satisfied interlock. One
    :class:`~veridex.dust_execution.contracts.OperatorInterlockEvent` is recorded per precondition,
    in the fixed :data:`OPERATOR_PRECONDITIONS` order (deterministic), carrying the operator's
    non-secret ``operator_authorization_ref`` and ``first_order_authorized`` assertion. The model
    RECORDS the operator's assertions (esp. jurisdiction/legal comfort) and concludes none of them.
    """
    effective = interlock if interlock is not None else OperatorInterlock()
    satisfied = effective.satisfied_by_precondition()
    events = tuple(
        OperatorInterlockEvent(
            sequence_no=index,
            event_type="OperatorInterlockEvent",
            source_ts=None,
            recv_ts=recv_ts_ms,
            precondition=name,
            satisfied=satisfied[name],
            operator_authorization_ref=effective.operator_authorization_ref,
            first_order_authorized=effective.first_order_authorized,
        )
        for index, name in enumerate(OPERATOR_PRECONDITIONS, start=1)
    )
    missing = tuple(name for name in OPERATOR_PRECONDITIONS if not satisfied[name])
    return OperatorInterlockGate(armed=not missing, missing=missing, events=events)


def _fail_closed_no_durable_state(
    request: MMExecutionToolRequest,
    *,
    envelope: PolicyEnvelope,
    emit: Callable[..., None],
) -> MMExecutionToolResult:
    """Return the Gate#3 MAJOR-2 fail-closed result for a live run with NO durable session-state source.

    Live mode must NOT proceed on a fresh/zero default: without a provider there is no authoritative
    session identity, no reconstructed realized loss, and no durable order counts, so the money path
    cannot be entered. This returns a typed NOT_ARMED/denied result WITHOUT calling the runner or the
    write port, and emits the honest terminal OPS telemetry. Carries only the pinned honest labels +
    closed-vocab reason codes (SEC-005), never a live handle.
    """
    tool_result = MMExecutionToolResult(
        admission="DENIED",
        reason_codes=("durable_session_state_unavailable",),
        execution_status="NOT_ARMED",
        execution_reason_codes=("mode_b_not_armed",),
        lifecycle_receipt_ref=f"dust-lifecycle:{request.session_id}:no-durable-session-state",
        run_label=_DEFAULT_RUN_LABEL,
        calibration_label=_DEFAULT_CALIBRATION_LABEL,
        edge_label=_DEFAULT_EDGE_LABEL,
        evidence_class=_DEFAULT_EVIDENCE_CLASS,
        policy_hash=envelope.policy_hash(),
    )
    emit(
        RuntimeEventType.ACTION_EMITTED,
        admission=tool_result.admission,
        execution_status=tool_result.execution_status,
        intent_kind=request.intent_kind,
    )
    emit(
        RuntimeEventType.RUN_COMPLETED,
        status=RuntimeStatus.COMPLETED.value,
        admission=tool_result.admission,
        reason_codes=list(tool_result.reason_codes),
        execution_status=tool_result.execution_status,
        execution_reason_codes=list(tool_result.execution_reason_codes),
        lifecycle_receipt_ref=tool_result.lifecycle_receipt_ref,
        session_status="FAILED",
        submitted_count=0,
    )
    return tool_result


def _fail_closed_session_identity_mismatch(
    request: MMExecutionToolRequest,
    *,
    envelope: PolicyEnvelope,
    emit: Callable[..., None],
) -> MMExecutionToolResult:
    """Return the Gate#3 R4-MAJOR-2 fail-closed result when the provider's durable state does NOT
    adopt the operator-assigned request identity.

    ``MMExecutionToolRequest.session_id`` is the immutable operator-assigned safety/ledger join key;
    the provider must merely ADOPT/echo it. When the durable state's ``session_identity`` is
    empty/absent or differs from ``request.session_id`` — or the supplied risk accumulator is bound to
    a different session — a stale/corrupt/mis-keyed provider response would swap the safety ledger and
    bypass the requested session's accumulated caps + realized loss. The money path must NOT be
    entered: no interlock is recorded, the runner is never reached, and no write port is called. This
    returns a typed NOT_ARMED/denied result and emits the honest terminal OPS telemetry, carrying only
    the pinned honest labels + closed-vocab reason codes (SEC-005), never a live handle.
    """
    tool_result = MMExecutionToolResult(
        admission="DENIED",
        reason_codes=("durable_session_identity_mismatch",),
        execution_status="NOT_ARMED",
        execution_reason_codes=("mode_b_not_armed",),
        lifecycle_receipt_ref=f"dust-lifecycle:{request.session_id}:session-identity-mismatch",
        run_label=_DEFAULT_RUN_LABEL,
        calibration_label=_DEFAULT_CALIBRATION_LABEL,
        edge_label=_DEFAULT_EDGE_LABEL,
        evidence_class=_DEFAULT_EVIDENCE_CLASS,
        policy_hash=envelope.policy_hash(),
    )
    emit(
        RuntimeEventType.ACTION_EMITTED,
        admission=tool_result.admission,
        execution_status=tool_result.execution_status,
        intent_kind=request.intent_kind,
    )
    emit(
        RuntimeEventType.RUN_COMPLETED,
        status=RuntimeStatus.COMPLETED.value,
        admission=tool_result.admission,
        reason_codes=list(tool_result.reason_codes),
        execution_status=tool_result.execution_status,
        execution_reason_codes=list(tool_result.execution_reason_codes),
        lifecycle_receipt_ref=tool_result.lifecycle_receipt_ref,
        session_status="FAILED",
        submitted_count=0,
    )
    return tool_result


def _reservation_attempt_id(request: MMExecutionToolRequest) -> str:
    """Derive a STABLE, deterministic per-attempt idempotency identity for the durable reservation.

    Gate#3 R5-MAJOR-1: the attempt_id binds the possibly-live attempt to the COMPLETE admitted-order
    fingerprint — the operator-assigned ``session_id``, the ``intent_kind``, the FULL ``intent_params``
    (token_id, side, price, size, tif, client_order_id, replaces_client_order_id), and the admission
    pins (``manifest_hash`` / ``policy_hash`` / ``strategy_config_hash`` / ``mode``), serialized
    deterministically — and is INDEPENDENT of any mutable durable count/index. So an IDENTICAL retry
    derives the SAME id (it reconciles to the existing reservation instead of minting a new id and
    double-submitting), while a genuinely DISTINCT authorized attempt (a different ``client_order_id`` /
    ``intent_params``) derives a DISTINCT id and reserves its own possibly-live slot.

    This replaces the earlier count-derived id (R4-MAJOR-1), whose monotonic ``attempt_index`` advanced
    the instant the first reservation was counted — so the SAME request derived a DIFFERENT id on retry
    and double-reserved/double-submitted whenever any spare session/day capacity existed. A reference
    string only — the fields are HASHED (never stored raw), and none is a secret or a live handle
    (SEC-005; the sizing ``intent_params`` are non-secret order parameters).
    """
    params = request.intent_params
    fingerprint = json.dumps(
        {
            "session_id": request.session_id,
            "intent_kind": request.intent_kind,
            "intent_params": {
                "token_id": params.token_id,
                "side": params.side,
                "price": params.price,
                "size": params.size,
                "tif": params.tif,
                "client_order_id": params.client_order_id,
                "replaces_client_order_id": params.replaces_client_order_id,
            },
            "manifest_hash": request.manifest_hash,
            "policy_hash": request.policy_hash,
            "strategy_config_hash": request.strategy_config_hash,
            "mode": request.mode,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return f"dust-attempt:{request.session_id}:{digest[:16]}"


def _reconciliation_outcome(
    result: DustExecutionResult,
) -> tuple[ReconciliationState, str | None]:
    """Reduce the runner's REAL run to (reservation reconciliation disposition, venue_order_key join).

    IDM-002 fail-closed reducer. The FREEZE keys on the reconciliation axis, NOT on wire-fired /
    ``submitted`` — a submitted order with AMBIGUOUS reconciliation is possibly-live and must FREEZE. The
    disposition is selected from the EXACT attempted decision(s) of THIS run, MATCHED on the EXACT PAIR
    ``(decision_id, venue_order_key)`` (Gate#3 R6-MAJOR-1), NEVER "any RESOLVED row anywhere in the
    result" and NEVER the ``venue_order_key`` ALONE: a multi-decision result could carry a benign RESOLVED
    row from a DIFFERENT decision that reuses the SAME key, and picking it by key alone would wrongly clear
    the freeze for an order that is still possibly-live. So for EACH decision that actually reached the
    wire (a non-``dry_run_not_submitted`` :class:`~veridex.dust_execution.contracts.OrderAckEvent`) the
    reducer joins the :class:`~veridex.dust_execution.contracts.OrderSubmitAttempt`'s
    ``(decision_id, presubmit_record.venue_order_key)`` to the
    :class:`~veridex.dust_execution.contracts.RealFillReconciliation` whose ``decision_id`` AND
    ``venue_order_key`` BOTH equal it:

      * NO decision reached the wire (a POSITIVELY-PROVEN no-wire abstention / dry-run) -> the only fast
        release: ``DEFINITIVELY_ABSENT``;
      * a submitted decision with NO pre-submit key, or NO row matching its exact ``(decision_id,
        venue_order_key)`` PAIR (MISSING / FOREIGN decision / MISMATCHED key), or DUPLICATE / MULTIPLE
        pair matches -> FAIL CLOSED: ``AMBIGUOUS`` (the reservation stays possibly-live / FROZEN — never
        released or resolved on ambiguity);
      * any matched row ``AMBIGUOUS`` -> ``AMBIGUOUS`` (FREEZE, counted);
      * every matched row venue-confirmed absent (``DEFINITIVELY-ABSENT``) -> ``DEFINITIVELY_ABSENT``
        (release);
      * else at least one matched row ``RESOLVED`` (definite fill) and none ambiguous -> ``RESOLVED``
        (counted, no longer freezing).

    Returns the disposition plus the single submitted decision's ``venue_order_key`` (or ``None`` when
    zero/multiple) so the caller can bind it to the reservation row (the production reconcile join). A
    closed-vocab, id-free disposition (SEC-005) — never a fill/PnL/rankable value.
    """
    submitted_decision_ids = {
        event.decision_id
        for event in result.events
        if isinstance(event, OrderAckEvent) and event.ack_status != "dry_run_not_submitted"
    }
    if not submitted_decision_ids:
        # A positively-proven no-wire abstention/dry-run: nothing possibly-live → release.
        return "DEFINITIVELY_ABSENT", None
    # Each submitted decision's OFFICIAL venue_order_key from its durable pre-submit record. A duplicate
    # attempt row for one decision, or a submitted decision with no/empty pre-submit key, FAILS CLOSED.
    submit_keys: dict[str, str] = {}
    for event in result.events:
        if isinstance(event, OrderSubmitAttempt) and event.decision_id in submitted_decision_ids:
            key = event.presubmit_record.venue_order_key
            if event.decision_id in submit_keys or not key:
                return "AMBIGUOUS", None
            submit_keys[event.decision_id] = key
    if set(submit_keys) != submitted_decision_ids:
        # A submitted decision with NO pre-submit record → cannot be keyed → FAIL CLOSED.
        return "AMBIGUOUS", None
    states: list[str] = []
    for decision_id, key in submit_keys.items():
        # Gate#3 R6-MAJOR-1: join reconciliation on the EXACT PAIR (decision_id, venue_order_key), NOT on
        # the key ALONE. A row matches ONLY when BOTH its ``decision_id`` and ``venue_order_key`` equal
        # THIS submitted decision's — so a FOREIGN decision's row that merely reuses this key (a
        # multi-decision result carrying a benign RESOLVED row for a DIFFERENT decision under the SAME
        # key) is NOT positive terminal proof for this order and can never clear its freeze.
        matches = [
            event
            for event in result.events
            if isinstance(event, RealFillReconciliation)
            and event.decision_id == decision_id
            and event.venue_order_key == key
        ]
        if len(matches) != 1:
            # MISSING / FOREIGN-decision / MISMATCHED-key / DUPLICATE / MULTIPLE pair rows → FAIL CLOSED.
            return "AMBIGUOUS", None
        states.append(matches[0].reconciled_state)
    join_key = next(iter(submit_keys.values())) if len(submit_keys) == 1 else None
    if any(state == "AMBIGUOUS" for state in states):
        return "AMBIGUOUS", join_key
    if all(state == "DEFINITIVELY-ABSENT" for state in states):
        # Every matched order is venue-confirmed ABSENT (never placed) → release (the E4 event spells it
        # hyphenated; the reservation axis uses the underscore form).
        return "DEFINITIVELY_ABSENT", join_key
    # At least one matched order reconciled RESOLVED (definite fill) and none is ambiguous: counted, no
    # longer freezing.
    return "RESOLVED", join_key


def _fail_closed_reservation_failed(
    request: MMExecutionToolRequest,
    *,
    envelope: PolicyEnvelope,
    emit: Callable[..., None],
) -> MMExecutionToolResult:
    """Return the Gate#3 R4-MAJOR-1 fail-closed result when the durable BEFORE-wire reservation fails.

    The possibly-live attempt must be durably RESERVED before the runner reaches the write port. When
    the reserve call raises (a durable-store outage), the money path must NOT be entered: the runner is
    never reached and no write port is called, so no possibly-live order can exist without a durable cap
    consumption. This returns a typed NOT_ARMED/denied result and emits the honest terminal OPS
    telemetry, carrying only the pinned honest labels + closed-vocab reason codes (SEC-005), never a
    live handle.
    """
    tool_result = MMExecutionToolResult(
        admission="DENIED",
        reason_codes=("durable_reservation_failed",),
        execution_status="NOT_ARMED",
        execution_reason_codes=("mode_b_not_armed",),
        lifecycle_receipt_ref=f"dust-lifecycle:{request.session_id}:reservation-failed",
        run_label=_DEFAULT_RUN_LABEL,
        calibration_label=_DEFAULT_CALIBRATION_LABEL,
        edge_label=_DEFAULT_EDGE_LABEL,
        evidence_class=_DEFAULT_EVIDENCE_CLASS,
        policy_hash=envelope.policy_hash(),
    )
    emit(
        RuntimeEventType.ACTION_EMITTED,
        admission=tool_result.admission,
        execution_status=tool_result.execution_status,
        intent_kind=request.intent_kind,
    )
    emit(
        RuntimeEventType.RUN_COMPLETED,
        status=RuntimeStatus.COMPLETED.value,
        admission=tool_result.admission,
        reason_codes=list(tool_result.reason_codes),
        execution_status=tool_result.execution_status,
        execution_reason_codes=list(tool_result.execution_reason_codes),
        lifecycle_receipt_ref=tool_result.lifecycle_receipt_ref,
        session_status="FAILED",
        submitted_count=0,
    )
    return tool_result


def _freeze_pending_reconciliation(
    request: MMExecutionToolRequest,
    *,
    envelope: PolicyEnvelope,
    emit: Callable[..., None],
) -> MMExecutionToolResult:
    """Return the Gate#3 R5-MAJOR-1 FREEZE result for an identical retry atop an UNSETTLED reservation.

    A prior possibly-live (reserved-but-unsettled) attempt already exists for this stable ``attempt_id``
    — an identical retry after a crash/outage. The retry must NOT re-fire the wire atop the possibly-live
    first order, REGARDLESS of spare session/day capacity: the money path is NOT entered (the runner is
    never reached, no write port is called), pending the production venue-truth reconcile.

    The disposition is an EXPLICIT pending-reconciliation / NOT_ARMED / DENIED terminal — NEVER a
    SUBMITTED/success "recovered" status. The offline freeze is a SAFE pending terminal, not a
    resolution: a future live-armed path must not read the frozen retry as resolved/complete until the
    production venue-truth resolver runs. Carries only the pinned honest labels + closed-vocab reason
    codes (SEC-005), never a live handle.
    """
    tool_result = MMExecutionToolResult(
        admission="DENIED",
        reason_codes=("attempt_pending_reconciliation",),
        execution_status="NOT_ARMED",
        execution_reason_codes=("mode_b_not_armed",),
        lifecycle_receipt_ref=(
            f"dust-lifecycle:{request.session_id}:attempt-pending-reconciliation"
        ),
        run_label=_DEFAULT_RUN_LABEL,
        calibration_label=_DEFAULT_CALIBRATION_LABEL,
        edge_label=_DEFAULT_EDGE_LABEL,
        evidence_class=_DEFAULT_EVIDENCE_CLASS,
        policy_hash=envelope.policy_hash(),
    )
    emit(
        RuntimeEventType.ACTION_EMITTED,
        admission=tool_result.admission,
        execution_status=tool_result.execution_status,
        intent_kind=request.intent_kind,
    )
    emit(
        RuntimeEventType.RUN_COMPLETED,
        status=RuntimeStatus.COMPLETED.value,
        admission=tool_result.admission,
        reason_codes=list(tool_result.reason_codes),
        execution_status=tool_result.execution_status,
        execution_reason_codes=list(tool_result.execution_reason_codes),
        lifecycle_receipt_ref=tool_result.lifecycle_receipt_ref,
        session_status="FAILED",
        submitted_count=0,
    )
    return tool_result


def _idempotent_committed_result(
    request: MMExecutionToolRequest,
    *,
    envelope: PolicyEnvelope,
    emit: Callable[..., None],
) -> MMExecutionToolResult:
    """Return the Gate#3 R5-MAJOR-1 IDEMPOTENT result for an identical retry atop a COMMITTED reservation.

    A prior attempt for this stable ``attempt_id`` already SETTLED committed (the first order reached the
    wire and was durably recorded). An identical retry replays that KNOWN-GOOD outcome WITHOUT re-firing
    the wire — a resolved terminal (distinct from the possibly-live FREEZE): the money path is NOT
    re-entered (the runner is never reached, no write port is called). Carries only the pinned honest
    labels + closed-vocab reason codes (SEC-005), never a live handle.
    """
    tool_result = MMExecutionToolResult(
        admission="APPROVED",
        reason_codes=("idempotent_committed_replay",),
        execution_status="SUBMITTED",
        execution_reason_codes=(),
        lifecycle_receipt_ref=(
            f"dust-lifecycle:{request.session_id}:idempotent-committed-replay"
        ),
        run_label=_DEFAULT_RUN_LABEL,
        calibration_label=_DEFAULT_CALIBRATION_LABEL,
        edge_label=_DEFAULT_EDGE_LABEL,
        evidence_class=_DEFAULT_EVIDENCE_CLASS,
        policy_hash=envelope.policy_hash(),
    )
    emit(
        RuntimeEventType.ACTION_EMITTED,
        admission=tool_result.admission,
        execution_status=tool_result.execution_status,
        intent_kind=request.intent_kind,
    )
    emit(
        RuntimeEventType.RUN_COMPLETED,
        status=RuntimeStatus.COMPLETED.value,
        admission=tool_result.admission,
        reason_codes=list(tool_result.reason_codes),
        execution_status=tool_result.execution_status,
        execution_reason_codes=list(tool_result.execution_reason_codes),
        lifecycle_receipt_ref=tool_result.lifecycle_receipt_ref,
        session_status="COMPLETED",
        submitted_count=1,
    )
    return tool_result


async def propose_mm_execution(
    request: MMExecutionToolRequest,
    *,
    adapter: VenueAdapter,
    signer: Signer,
    sources: QuoteSource,
    now_fn: Callable[[], int],
    sleep_fn: Callable[[float], Awaitable[None]],
    envelope: PolicyEnvelope,
    manifest: StrategyExperimentManifest,
    wallet_equity_at_decision: float,
    fixed_fraction: float,
    arming: ModeBArming | None = None,
    operator_interlock: OperatorInterlock | None = None,
    interlock_store: OperatorInterlockStore | None = None,
    provider: DurableSessionStateProvider | None = None,
    event_sink: RuntimeEventSink | None = None,
    agent_id: str = _FACADE_AGENT_ID,
    run_id: str | None = None,
) -> MMExecutionToolResult:
    """Injectable R4-B intent -> R4-A execute proposer/adapter (REQ-003, AC-019/020/026).

    Takes a typed :class:`MMExecutionToolRequest`, drives R4-A admission/execution/reconciliation
    THROUGH :func:`~veridex.dust_execution.runner.run_dust_execution` (imported LAZILY here to break
    the runner<->facade import cycle), and returns a typed :class:`MMExecutionToolResult` + an OPAQUE
    lifecycle receipt REF. The runner cross-checks the request's DECLARED hashes against the admitted
    pins and fails closed on any mismatch; its ``confidence`` / requested ``size`` are untrusted
    metadata that NEVER reach the wire.

    This is an INJECTABLE adapter that the operator / R4-B wires in — it is deliberately NOT a
    callable tool on the ``tools=[]`` decision agent. Its lifecycle emits ONLY into the injected OPS
    :class:`~veridex.runtime.runtime_events.RuntimeEvent` sink (``event_sink``); the payloads carry
    only boolean / id / closed-vocab telemetry + the receipt REF — no secret, no raw handle (SEC-005).

    R4-A ships safety-complete WITHOUT R4-B: a pinned ``EXPERIMENTAL_DUST`` manifest alone admits and
    runs here — no real/promoted strategy or alpha is required. Mode B stays UNARMED unless a fully
    passing ``arming`` bundle is supplied (fail-closed).

    Args:
        request: The typed, hash-pinned agent intent (R4-B proposes; R4-A executes).
        adapter: Injected venue adapter (a recording-fake offline; never a live venue in R4-A tests).
        signer: Injected provider-neutral signing control plane (Mode-A fake offline).
        sources: Injected quote source (raises on a gapped/disconnected book).
        now_fn: Injected integer-seconds clock seam.
        sleep_fn: Injected async delay seam (never a real wall-clock wait).
        envelope: Policy envelope providing caps, quote-age, kill switch, and the admitted policy hash.
        manifest: Pinned strategy manifest providing the token universe + admitted manifest hash.
        wallet_equity_at_decision: PINNED mechanical sizing input (never agent-supplied).
        fixed_fraction: PINNED mechanical sizing input (never agent-supplied).
        arming: Optional Mode-B arming bundle; ``None`` (default) keeps Mode B UNARMED (fail closed).
        operator_interlock: The E7-T3 human operator precondition interlock (REQ-005/006, AC-002).
            When Mode B is being armed (``arming`` is supplied), ALL FIVE operator preconditions must
            be positively satisfied; a missing one is an explicit no-go that WITHHOLDS the arming
            bundle so Mode B stays UNARMED. ``None`` is treated fail-closed (all-unsatisfied).
        interlock_store: The durable :class:`~veridex.dust_execution.operator_interlock_store.
            OperatorInterlockStore` that ISSUES and STORE-VERIFIES the interlock receipt (the REQ-005
            audit trail). MANDATORY to arm Mode B (Gate#3 MAJOR-1 + M-1): REQ-005 requires the
            interlock be satisfied AND durably PERSISTED, and the receipt must be UNFORGEABLE — the
            store must actually ISSUE it. When Mode B is being armed a ``None`` store (or one that does
            not durably persist) FAILS CLOSED (the arming bundle is withheld — no arm, no submit); the
            SAME store is threaded into the runner, which re-VERIFIES the receipt against the actual
            session/events/auth/attempt before arming. An offline test injects an in-memory store; the
            production arming path refuses to arm without a real one.
        provider: The injected :class:`~veridex.dust_execution.session_state.DurableSessionStateProvider`
            — ONE authoritative durable session-state source (Gate#3 MAJOR-2). BEFORE any live arming it
            supplies the operator-assigned IMMUTABLE session identity (``request.session_id``, adopted as
            the safety/ledger join key), the reconstructed realized-loss ``RiskAccumulator``, and the
            persisted session/UTC-day possibly-live attempt counts; the facade threads them into the
            runner so its order/loss caps are enforced from HONEST durable inputs (never reset to
            fresh/zero each call), then persists this run's attempt-count delta back through the SAME
            identity. ``live_guarded`` with ``provider=None`` FAILS CLOSED (NOT_ARMED/denied, no
            write-port I/O); a non-live Mode-A dry-run may keep the documented fresh default.
        event_sink: Optional OPS ``RuntimeEvent`` sink; when ``None`` the proposer emits nothing.
        agent_id: Non-secret OPS ``agent_id`` label stamped on emitted telemetry.
        run_id: Optional OPS correlation id for the emitted lifecycle events.

    Returns:
        A typed :class:`MMExecutionToolResult` (admission verdict + reason codes + honest labels +
        an opaque lifecycle receipt REF + admitted ``policy_hash``).
    """
    from veridex.dust_execution.runner import (  # lazy: breaks the runner<->facade import cycle
        OperatorInterlockProof,
        _arming_attempt_ref,
        provisional_session_id,
        run_dust_execution,
    )

    def _emit(event_type: RuntimeEventType, **payload: object) -> None:
        if event_sink is None:
            return
        event_sink(
            runtime_event(
                event_type,
                agent_id=agent_id,
                run_id=run_id,
                session_id=request.session_id,
                **payload,
            )
        )

    _emit(RuntimeEventType.RUN_STARTED, intent_kind=request.intent_kind, mode=request.mode)
    _emit(RuntimeEventType.STATUS_CHANGED, status=RuntimeStatus.RUNNING.value)

    # Gate#3 MAJOR-2: compose ONE authoritative durable session-state source BEFORE any live arming.
    # The injected provider supplies (from durable storage) the operator-assigned IMMUTABLE session
    # identity, the reconstructed realized-loss :class:`RiskAccumulator` (prior session + UTC-day loss),
    # and the persisted session/UTC-day possibly-live attempt counts — so the runner enforces its
    # run/session/day order caps and realized-loss caps from HONEST durable inputs, never a fresh/zero
    # default that resets on every call (the MAJOR-2 hole: two same-session calls both reaching the
    # write port, and a prior realized loss never reconstructed before the next arming — SAF-002).
    now_dt = datetime.fromtimestamp(now_fn(), tz=UTC)
    durable_state = (
        provider.load(session_id=request.session_id, now=now_dt) if provider is not None else None
    )

    # Live mode (``live_guarded``) FAILS CLOSED when the durable source is absent: a real-money run must
    # NEVER proceed on a fresh/zero default. Return a NOT_ARMED/denied typed result WITHOUT reaching the
    # runner or the write port. A non-live Mode-A dry-run keeps the documented default (it places no
    # orders, so a fresh accumulator is harmless there).
    if request.mode == "live_guarded" and durable_state is None:
        return _fail_closed_no_durable_state(request, envelope=envelope, emit=_emit)

    # Gate#3 R4-MAJOR-2: the provider is trusted to ADOPT/echo the operator-assigned IMMUTABLE identity,
    # never to SUBSTITUTE its own. Before the substituted identity can bind the interlock receipt or
    # supply the runner's risk/count namespace, REQUIRE that the durable state actually adopted this
    # request's identity: a non-empty ``session_identity`` that EQUALS ``request.session_id``, AND a
    # risk accumulator bound to that SAME session. A stale/corrupt/mis-keyed provider response that
    # swaps the identity would bind the run to a DIFFERENT safety ledger and bypass the requested
    # session's accumulated caps + realized loss — so any mismatch FAILS CLOSED here, before recording
    # the interlock or entering the runner (no write-port I/O).
    if durable_state is not None and (
        not durable_state.session_identity
        or durable_state.session_identity != request.session_id
        or durable_state.risk.session_id != request.session_id
    ):
        return _fail_closed_session_identity_mismatch(request, envelope=envelope, emit=_emit)

    # The AUTHORITATIVE session identity the runner runs under: the provider's operator-assigned
    # immutable identity when present, else the provisional ``(strategy_id, mode)`` seam (Mode-A default).
    session_identity = (
        durable_state.session_identity
        if durable_state is not None
        else provisional_session_id(manifest, request.mode)
    )

    # Gate#3 R4-MAJOR-1 + R5-MAJOR-1: durably RESERVE-OR-LOAD a possibly-live attempt BEFORE the runner
    # reaches the write port — the durable-cap analog of the lane's persist-BEFORE-sign discipline. The
    # reservation counts toward the session/UTC-day caps IMMEDIATELY and durably (``load`` counts
    # reserved-but-unsettled rows), so a durable-store failure or a process crash AFTER the wire cannot
    # reset the cap: the reserved row stands and the NEXT call sees it. If the reserve RAISES (store
    # outage) the money path is NOT entered — the runner is never reached and no write port is called
    # (fail closed). The ``attempt_id`` is a STABLE idempotency identity derived from the COMPLETE
    # request fingerprint (R5-MAJOR-1), INDEPENDENT of any mutable durable count — so an IDENTICAL retry
    # derives the SAME id and RECONCILES instead of minting a new id and double-submitting:
    #   * an existing UNSETTLED (possibly-live) row  -> FREEZE unconditionally (no new wire atop a
    #     possibly-live first order, regardless of spare cap), pending the production venue-truth
    #     reconcile — an EXPLICIT pending-reconciliation/denied terminal, never a success;
    #   * an existing COMMITTED row                  -> idempotent replay of the first outcome (no wire);
    #   * a fresh RESERVED slot (or a prior RELEASED attempt, now absent) -> proceed to the runner.
    # Only on the durable-provider path (fresh/zero Mode-A dry-runs place no orders, nothing to reserve).
    #
    # IDM-002 adds a SECOND, session-SCOPED layer to the id-dedup: stable-id dedup alone is insufficient
    # because a caller could change ``client_order_id`` / token / any intent field to derive a DISTINCT
    # ``attempt_id`` and submit AROUND an unresolved first order (still double exposure). So a possibly-live
    # reservation whose reconciliation is UNRESOLVED (PENDING / AMBIGUOUS) anywhere in the session must
    # FREEZE EVERY new submit — INCLUDING a genuinely distinct id — until it resolves (venue-truth reconcile,
    # the production hook) or an operator clears it. Only an UNSETTLED reservation freezes; a COMMITTED or
    # RELEASED one does not, so an honest reserve->wire->settle(committed=True) sequence never self-blocks.
    #
    # Gate#3 R6-MAJOR-2: the scope-freeze check and the reserve are ONE ATOMIC provider op
    # (``reserve_or_freeze``), NOT a separate ``has_unresolved_reservation`` followed by ``reserve``. The
    # split was a TOCTOU: no transaction spanned the pair, so two concurrent requests could BOTH observe an
    # empty scope and BOTH reserve+wire. The single op fuses the scope check with the reserve-or-load in one
    # critical section and DISCRIMINATES its outcome:
    #   * SCOPE_FROZEN       -> a DIFFERENT id while an unresolved possibly-live reservation occupies the
    #                           scope: FREEZE (no new wire, no spurious row), pending venue-truth reconcile;
    #   * PENDING_RECONCILE  -> an identical retry atop THIS attempt's own possibly-live first order: FREEZE;
    #   * COMMITTED          -> an identical retry atop THIS attempt's committed first order: idempotent
    #                           replay (no new wire);
    #   * RESERVED           -> a fresh slot (or a prior RELEASED attempt, now absent) -> proceed to the runner.
    # A raise (durable-store outage) FAILS CLOSED — the runner is never reached and no write port is called.
    # Only on the durable-provider path (fresh/zero Mode-A dry-runs place no orders, nothing to reserve).
    attempt_id: str | None = None
    if provider is not None and durable_state is not None:
        attempt_id = _reservation_attempt_id(request)
        try:
            reservation_outcome = provider.reserve_or_freeze(
                session_identity=session_identity, now=now_dt, attempt_id=attempt_id
            )
        except Exception:
            return _fail_closed_reservation_failed(request, envelope=envelope, emit=_emit)
        if reservation_outcome in ("SCOPE_FROZEN", "PENDING_RECONCILE"):
            # SCOPE_FROZEN: a DISTINCT id blocked by an unresolved possibly-live reservation in the session
            # scope. PENDING_RECONCILE: an identical retry atop this attempt's own possibly-live first order.
            # Either way FREEZE — no new wire atop a possibly-live order, pending the venue-truth reconcile.
            return _freeze_pending_reconciliation(request, envelope=envelope, emit=_emit)
        if reservation_outcome == "COMMITTED":
            # An identical retry atop a committed first order: idempotent replay, no new wire.
            return _idempotent_committed_result(request, envelope=envelope, emit=_emit)

    # E7-T3 human operator precondition interlock (REQ-005/006, AC-002) + Gate#3 MAJOR-1 & M-1: Mode B
    # cannot ARM unless ALL FIVE operator preconditions are positively satisfied AND durably PERSISTED
    # via an UNFORGEABLE, STORE-ISSUED receipt. This is enforced on BOTH legs:
    #   (1) DURABLE, STORE-ISSUED RECEIPT — REQ-005 requires "satisfied AND recorded", and M-1 requires
    #       the recording be UNFORGEABLE: callback presence and a self-computed digest are NOT evidence
    #       of a write. The facade records the events into the injected durable
    #       :class:`OperatorInterlockStore` and takes the receipt the STORE ISSUES. A ``None`` store
    #       (nowhere to persist) yields NO receipt -> the arming bundle is WITHHELD (no arm, no submit).
    #   (2) UNBYPASSABLE, STORE-VERIFIABLE BINDING — on a fully-satisfied+recorded interlock the facade
    #       BINDS an ``OperatorInterlockProof`` (carrying the STORE-ISSUED receipt + the recorded events
    #       + operator-auth ref) INTO the arming artifact, AND threads the SAME store into the runner,
    #       which re-VERIFIES the receipt against the ACTUAL session/events/auth/attempt. So a DIRECT
    #       ``run_dust_execution`` with a technical-only or FORGED-receipt bundle stays UNARMED. A
    #       missing precondition WITHHOLDS the bundle. Never a parallel arming path — the runner's
    #       EXISTING gate (now store-verified) is the single enforcer.
    effective_arming = arming
    if request.mode == "live_guarded" and arming is not None:
        interlock_gate = evaluate_operator_interlock(operator_interlock, recv_ts_ms=now_fn() * 1000)
        operator_auth_ref = (
            interlock_gate.events[0].operator_authorization_ref if interlock_gate.events else None
        )
        # Only a durable store can ISSUE a receipt; a ``None`` store cannot record -> no receipt. The
        # receipt is NEVER self-computed here — the store issues it, bound to the SAME session identity
        # the runner will run under (its provisional session id), the ordered events, the operator-auth
        # ref, and this run's arming-attempt ref.
        receipt: str | None = None
        # Gate#3 MAJOR-1: only record (and thus arm) when the events are the canonical-5 SEMANTICS the
        # store + runner both enforce — the SAME shared validator. ``armed`` already requires all five
        # satisfied, but the validator additionally requires a consistent NON-EMPTY operator-auth ref
        # (and canonical order/sequence), so an armed-but-refless interlock withholds the bundle here
        # rather than tripping the store's fail-closed refusal.
        if (
            interlock_gate.armed
            and interlock_store is not None
            and interlock_events_are_canonical(interlock_gate.events)
        ):
            receipt = interlock_store.record(
                # Gate#3 MAJOR-2: bind the receipt to the AUTHORITATIVE session identity the runner runs
                # under (the provider's immutable id), so the store-issued receipt verifies against the
                # SAME ``session.session_id`` the runner threads — not the provisional seam.
                session_id=session_identity,
                events=interlock_gate.events,
                operator_authorization_ref=operator_auth_ref,
                arming_attempt_ref=_arming_attempt_ref(arming),
            )
        # Arm ONLY when every precondition is satisfied AND the store durably ISSUED a receipt; then
        # BIND the store-issued proof into the arming artifact the runner consumes. Otherwise WITHHOLD
        # the bundle — a missing precondition OR a missing/None store is an explicit no-go — so the
        # runner's existing (store-verified) gate keeps Mode B UNARMED (fail closed).
        if interlock_gate.armed and receipt is not None:
            effective_arming = replace(
                arming,
                operator_interlock=OperatorInterlockProof(
                    satisfied=True,
                    recording_receipt=receipt,
                    events=interlock_gate.events,
                    operator_authorization_ref=operator_auth_ref,
                ),
            )
        else:
            effective_arming = None

    result = await run_dust_execution(
        adapter=adapter,
        signer=signer,
        sources=sources,
        now_fn=now_fn,
        sleep_fn=sleep_fn,
        envelope=envelope,
        manifest=manifest,
        mode=request.mode,
        wallet_equity_at_decision=wallet_equity_at_decision,
        fixed_fraction=fixed_fraction,
        request=request,
        arming=effective_arming,
        operator_interlock_store=interlock_store,
        # Gate#3 MAJOR-2: the authoritative identity + the durable risk/counts the runner enforces its
        # caps against (fresh/zero ONLY on the non-live default path where ``durable_state`` is None).
        session_identity=session_identity,
        risk=durable_state.risk if durable_state is not None else None,
        prior_session_order_count=(
            durable_state.prior_session_order_count if durable_state is not None else 0
        ),
        prior_day_order_count=(
            durable_state.prior_day_order_count if durable_state is not None else 0
        ),
    )

    # Gate#3 R4-MAJOR-1 + R5-MAJOR-1 / IDM-002 settle: record the reserved attempt's RECONCILIATION
    # outcome AFTER the run — the E4 tri-state derived from the runner's REAL run, NEVER a mere
    # wire/``submitted`` flag. A submitted order whose reconciliation is AMBIGUOUS stays possibly-live and
    # KEEPS FREEZING the session scope; only a RESOLVED (definite fill) reconciliation stops the freeze
    # (the slot stays counted); a DEFINITIVELY_ABSENT (abstain-no-wire / venue-confirmed absent) RELEASES
    # it. A ``settle`` failure AFTER a fired wire must NOT reset the reservation — the reserved row is
    # already durable and stays ``PENDING`` (possibly-live, freezing, counted) — so any settle exception
    # is swallowed: the standing reservation is the crash-consistent, fail-safe outcome (over-freezing a
    # possibly-live attempt, never under-freezing). R4-A's sealed lifecycle carries no ``realized_pnl``
    # (SEC-002), so realized-fill LOSS is reconstructed by the provider's own durable
    # venue-reconciliation ledger, never fabricated here.
    if provider is not None and durable_state is not None and attempt_id is not None:
        recon_state, recon_venue_order_key = _reconciliation_outcome(result)
        with contextlib.suppress(Exception):
            provider.settle(
                attempt_id=attempt_id,
                recon_state=recon_state,
                venue_order_key=recon_venue_order_key,
            )

    tool_result = _to_tool_result(result, request)

    # The proposer ran end-to-end and produced a typed result, so the OPS run is RUN_COMPLETED — the
    # admission verdict and the SAFETY-derived ``session_status`` (a bounded but AMBIGUOUS-reconciled
    # dry run is a "FAILED" safety outcome, per E6-T7) are reported as DATA in the payload, never as a
    # runtime failure of the proposer itself. RUN_FAILED is reserved for an actual runner exception.
    _emit(
        RuntimeEventType.ACTION_EMITTED,
        admission=tool_result.admission,
        execution_status=tool_result.execution_status,
        intent_kind=request.intent_kind,
    )
    _emit(
        RuntimeEventType.RUN_COMPLETED,
        status=RuntimeStatus.COMPLETED.value,
        admission=tool_result.admission,
        reason_codes=list(tool_result.reason_codes),
        execution_status=tool_result.execution_status,
        execution_reason_codes=list(tool_result.execution_reason_codes),
        lifecycle_receipt_ref=tool_result.lifecycle_receipt_ref,
        session_status=result.session_outcome.status,
        submitted_count=result.submitted_count,
    )
    return tool_result
