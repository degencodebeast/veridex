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
from typing import TYPE_CHECKING, Literal

from pydantic import field_validator

from veridex.dust_execution.contracts import (
    DustRunLabelEvent,
    EvidenceClass,
    ExecutionMode,
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
Admission = Literal["APPROVED", "DENIED", "REQUIRES_HUMAN"]


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
    """

    admission: Admission
    reason_codes: tuple[str, ...]
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


def _to_tool_result(
    result: DustExecutionResult, request: MMExecutionToolRequest
) -> MMExecutionToolResult:
    """Map the runner's :class:`DustExecutionResult` onto the typed boundary result (AC-020).

    Carries ONLY the admission verdict, ordered reason codes, an OPAQUE lifecycle receipt REF, the
    honest labels the run actually emitted, and the admitted ``policy_hash`` — NEVER the runner
    result object, adapter, signer, or any live handle. An ``ALLOW`` admission maps to ``APPROVED``,
    any ``DENY`` to ``DENIED``.
    """
    admission: Admission = "APPROVED" if result.admission.verdict == "ALLOW" else "DENIED"
    label = _terminal_label(result)
    return MMExecutionToolResult(
        admission=admission,
        reason_codes=result.admission.reason_codes,
        lifecycle_receipt_ref=_lifecycle_receipt_ref(result),
        run_label=label.run_label if label is not None else _DEFAULT_RUN_LABEL,
        calibration_label=(
            label.calibration_label if label is not None else _DEFAULT_CALIBRATION_LABEL
        ),
        edge_label=label.edge_label if label is not None else _DEFAULT_EDGE_LABEL,
        evidence_class=label.evidence_class if label is not None else request.evidence_class,
        policy_hash=result.admission.policy_hash,
    )


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
        event_sink: Optional OPS ``RuntimeEvent`` sink; when ``None`` the proposer emits nothing.
        agent_id: Non-secret OPS ``agent_id`` label stamped on emitted telemetry.
        run_id: Optional OPS correlation id for the emitted lifecycle events.

    Returns:
        A typed :class:`MMExecutionToolResult` (admission verdict + reason codes + honest labels +
        an opaque lifecycle receipt REF + admitted ``policy_hash``).
    """
    from veridex.dust_execution.runner import (  # lazy: breaks the runner<->facade import cycle
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
        arming=arming,
    )

    tool_result = _to_tool_result(result, request)

    # The proposer ran end-to-end and produced a typed result, so the OPS run is RUN_COMPLETED — the
    # admission verdict and the SAFETY-derived ``session_status`` (a bounded but AMBIGUOUS-reconciled
    # dry run is a "FAILED" safety outcome, per E6-T7) are reported as DATA in the payload, never as a
    # runtime failure of the proposer itself. RUN_FAILED is reserved for an actual runner exception.
    _emit(
        RuntimeEventType.ACTION_EMITTED,
        admission=tool_result.admission,
        intent_kind=request.intent_kind,
    )
    _emit(
        RuntimeEventType.RUN_COMPLETED,
        status=RuntimeStatus.COMPLETED.value,
        admission=tool_result.admission,
        reason_codes=list(tool_result.reason_codes),
        lifecycle_receipt_ref=tool_result.lifecycle_receipt_ref,
        session_status=result.session_outcome.status,
        submitted_count=result.submitted_count,
    )
    return tool_result
