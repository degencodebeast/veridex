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

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from pydantic import field_validator

from veridex.dust_execution.contracts import (
    DustRunLabelEvent,
    EvidenceClass,
    ExecutionMode,
    OperatorInterlockEvent,
    TimeInForce,
    _FrozenModel,
    _reject_price_out_of_unit_interval,
)
from veridex.runtime.runtime_events import RuntimeEventType, RuntimeStatus, runtime_event

if TYPE_CHECKING:
    from veridex.dust_execution.manifest import StrategyExperimentManifest
    from veridex.dust_execution.runner import DustExecutionResult, ModeBArming, QuoteSource
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

#: Precondition (a): an isolated FUNDED execution wallet is provisioned (SAF human gate).
PRECONDITION_ISOLATED_FUNDED_WALLET = "isolated_funded_wallet"
#: Precondition (b): the OPERATOR asserts their own jurisdiction/legal comfort. The model RECORDS
#: this assertion and makes NO jurisdiction/legal conclusion on the operator's behalf (REQ-006).
PRECONDITION_JURISDICTION_COMFORT = "operator_jurisdiction_comfort"
#: Precondition (c): a positive max capital-at-risk is DECLARED by the operator.
PRECONDITION_MAX_CAPITAL_AT_RISK = "declared_max_capital_at_risk"
#: Precondition (d): the operator confirms kill-switch readiness.
PRECONDITION_KILL_SWITCH_READY = "kill_switch_ready"
#: Precondition (e): the operator EXPLICITLY authorizes the first order.
PRECONDITION_FIRST_ORDER_AUTHORIZED = "first_order_authorized"

#: The five human operator preconditions, in the fixed recording order (deterministic audit trail).
OPERATOR_PRECONDITIONS: tuple[str, ...] = (
    PRECONDITION_ISOLATED_FUNDED_WALLET,
    PRECONDITION_JURISDICTION_COMFORT,
    PRECONDITION_MAX_CAPITAL_AT_RISK,
    PRECONDITION_KILL_SWITCH_READY,
    PRECONDITION_FIRST_ORDER_AUTHORIZED,
)


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


def _interlock_recording_receipt(
    session_id: str, events: tuple[OperatorInterlockEvent, ...]
) -> str:
    """Derive an OPAQUE, deterministic receipt REF proving the interlock events were DURABLY recorded.

    Gate#3 MAJOR-1 (REQ-005): the arming path binds this receipt into the runner's
    :class:`~veridex.dust_execution.runner.OperatorInterlockProof` ONLY after every
    :class:`OperatorInterlockEvent` was pushed to the (mandatory) recording sink, so a satisfied but
    UNRECORDED interlock cannot arm. The ref pins the session identity + the recorded preconditions
    via a sha256 digest — a REFERENCE STRING only, never a secret or live handle (SEC-005).
    """
    canonical = json.dumps(
        {
            "session_id": session_id,
            "recorded": [(event.sequence_no, event.precondition, event.satisfied) for event in events],
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"operator-interlock:{session_id}:{digest[:16]}"


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
    interlock_sink: Callable[[OperatorInterlockEvent], None] | None = None,
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
        interlock_sink: Sink recording each :class:`OperatorInterlockEvent` (the REQ-005 audit
            trail). MANDATORY to arm Mode B (Gate#3 MAJOR-1): REQ-005 requires the interlock be
            satisfied AND durably RECORDED, so when Mode B is being armed a ``None`` sink FAILS CLOSED
            (the arming bundle is withheld — no arm, no submit). An offline test injects a recording
            sink; the production arming path refuses to arm without one.
        event_sink: Optional OPS ``RuntimeEvent`` sink; when ``None`` the proposer emits nothing.
        agent_id: Non-secret OPS ``agent_id`` label stamped on emitted telemetry.
        run_id: Optional OPS correlation id for the emitted lifecycle events.

    Returns:
        A typed :class:`MMExecutionToolResult` (admission verdict + reason codes + honest labels +
        an opaque lifecycle receipt REF + admitted ``policy_hash``).
    """
    from veridex.dust_execution.runner import (  # lazy: breaks the runner<->facade import cycle
        OperatorInterlockProof,
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

    # E7-T3 human operator precondition interlock (REQ-005/006, AC-002) + Gate#3 MAJOR-1: Mode B cannot
    # ARM unless ALL FIVE operator preconditions are positively satisfied AND durably RECORDED. This is
    # enforced on BOTH legs:
    #   (1) MANDATORY RECORDING (fail-closed sink) — REQ-005 requires "satisfied AND recorded", so a
    #       ``None`` sink (nowhere to durably record) WITHHOLDS the arming bundle: no arm, no submit.
    #       An offline test injects a recording sink; the production arming path refuses without one.
    #   (2) UNBYPASSABLE BINDING — on a fully-satisfied+recorded interlock the facade BINDS an
    #       ``OperatorInterlockProof`` (carrying the durable-recording receipt) INTO the arming
    #       artifact the runner consumes, so a DIRECT ``run_dust_execution`` with a technical-only
    #       bundle (no proof) stays UNARMED. A missing precondition WITHHOLDS the bundle (explicit
    #       no-go). Never a parallel arming path — the runner's EXISTING gate is the single enforcer.
    effective_arming = arming
    if request.mode == "live_guarded" and arming is not None:
        interlock_gate = evaluate_operator_interlock(operator_interlock, recv_ts_ms=now_fn() * 1000)
        # REQ-005 requires the interlock be RECORDED: a ``None`` sink has nowhere to durably record,
        # so the interlock is NOT recorded. We only push events (and can only claim a durable receipt)
        # when a real sink is present.
        recorded = interlock_sink is not None
        if interlock_sink is not None:
            for interlock_event in interlock_gate.events:
                interlock_sink(interlock_event)
        # Arm ONLY when every precondition is satisfied AND the interlock was durably recorded; then
        # BIND the proof (with its recording receipt) into the arming artifact the runner consumes.
        # Otherwise WITHHOLD the bundle — a missing precondition OR a missing recording sink is an
        # explicit no-go — so the runner's existing gate keeps Mode B UNARMED (fail closed).
        if interlock_gate.armed and recorded:
            effective_arming = replace(
                arming,
                operator_interlock=OperatorInterlockProof(
                    satisfied=True,
                    recording_receipt=_interlock_recording_receipt(
                        request.session_id, interlock_gate.events
                    ),
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
