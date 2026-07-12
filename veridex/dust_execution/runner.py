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
``resolve_dust_size`` binding + native→decimal pricing still using the E6-T1 placeholder price/size
(E6-T4); a durable operator-assigned ``session_id`` and the sealed ``content_hash`` at session end
(E6-T5 startup sweep / E6-T6 shutdown); the real EIP-712 V2 order-hash (``venue_order_key``) binding
via ``veridex.dust_execution.signing_compiler`` (a later task — this module's placeholder is
distinct from the private integrity digest, never equal to it). The Mode B order built here still
uses PROVISIONAL price/size placeholders purely to exercise the (recording-fake, offline) submit
wire the gates protect — real sizing/pricing binding is E6-T4.

SEC-003: this module imports only intra-lane ``veridex.dust_execution.*``, the shared
``veridex.policy.envelope`` (the single breach-boundary source of truth, not a ranked lane), and
``veridex.venues.base`` (the pure adapter Protocol/value types) — never ``veridex.live_recorder``
and never a ranked maker/scoring/leaderboard module. :class:`StaleVenueBook` is defined IN-LANE
(the live-recorder lane owns its own same-named exception; this is a copy, not an import).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from veridex.dust_execution.contracts import (
    DustExecutionSessionMeta,
    DustRunLabelEvent,
    ExecutionMode,
    OrderAckEvent,
    OrderStatusEvent,
    OrderSubmitAttempt,
    OrderSubmitIntent,
    PreSubmitRecord,
    RealFillReconciliation,
    SessionRiskSnapshot,
)
from veridex.dust_execution.manifest import StrategyExperimentManifest
from veridex.dust_execution.signer import Signer, SigningPayload
from veridex.policy.envelope import PolicyEnvelope
from veridex.venues.base import Order, VenueAdapter

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
AbstainReason = Literal[
    "stale_quote_age",
    "stale_source",
    "event_suspended",
    "no_quote",
    "missing_book_side",
    "negative_liquidity",
    "mode_a_no_orders",
]

#: Tuple form of :data:`AbstainReason` for membership checks / iteration.
ABSTAIN_REASONS: tuple[AbstainReason, ...] = (
    "stale_quote_age",
    "stale_source",
    "event_suspended",
    "no_quote",
    "missing_book_side",
    "negative_liquidity",
    "mode_a_no_orders",
)


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


#: The E1-T2 numbered lifecycle-event union this runner emits (session meta precedes it, unnumbered
#: — :class:`DustExecutionSessionMeta` carries no ``sequence_no``). Ordered per event, one variant
#: per stage: risk snapshot, intent, attempt, ack, status, fill/reconciliation, labels.
LifecycleEvent = (
    SessionRiskSnapshot
    | OrderSubmitIntent
    | OrderSubmitAttempt
    | OrderAckEvent
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
    """

    mode: ExecutionMode
    decisions: tuple[SubmitDecision, ...]
    session_meta: DustExecutionSessionMeta
    events: tuple[LifecycleEvent, ...]

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


def _build_session_meta(
    *,
    manifest: StrategyExperimentManifest,
    envelope: PolicyEnvelope,
    signer: Signer,
    mode: ExecutionMode,
) -> DustExecutionSessionMeta:
    """Session identity/provenance preamble (unnumbered — carries no ``sequence_no``).

    PROVISIONAL SEAM: ``session_id`` is derived from ``(strategy_id, mode)`` — a durable,
    operator-assigned session identity and the sealed ``content_hash`` are wired by later tasks
    (E6-T5 startup sweep / E6-T6 shutdown). ``wallet_ref`` is the signer's own non-secret provider
    label (never a key/address). Every other field is REAL, sourced directly from the pinned
    ``manifest`` / ``envelope``.
    """
    return DustExecutionSessionMeta(
        session_id=f"{manifest.strategy_id}:{mode}",  # PROVISIONAL — real session identity: later task
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


def _build_risk_snapshot(*, seq: int, now_ms: int, envelope: PolicyEnvelope) -> SessionRiskSnapshot:
    """Session-level risk snapshot (``decision_id=None``) — first event in the numbered stream.

    PROVISIONAL SEAM: the realized-loss accumulator / open-order-count / breaker wiring
    (``veridex.dust_execution.risk.RiskAccumulator`` + ``emergency.SafetyController``) is E6-T3's
    job; this snapshot reports honest ZERO placeholders for those fields — never a fabricated
    non-zero value. ``kill_switch_engaged`` is REAL today: ``envelope.kill_switch`` is already
    available to the runner.
    """
    return SessionRiskSnapshot(
        sequence_no=seq,
        event_type="SessionRiskSnapshot",
        source_ts=None,
        recv_ts=now_ms,
        decision_id=None,
        realized_loss_session=0.0,  # PROVISIONAL — real accumulator wiring: E6-T3
        realized_loss_daily=0.0,  # PROVISIONAL — real accumulator wiring: E6-T3
        open_order_count=0,  # PROVISIONAL — real accumulator wiring: E6-T3
        breaker_open=False,  # PROVISIONAL — real accumulator wiring: E6-T3
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

    ``sleep_fn`` is the injected async delay seam for the E6 polling loop (added by a later task);
    this skeleton makes a single deterministic pass and does not sleep. The Mode B order uses
    PROVISIONAL price/size placeholders purely to exercise the (offline recording-fake) submit wire
    the gates protect — real sizing/pricing (``resolve_dust_size`` + native→decimal) is E6-T4.

    Args:
        adapter: Injected venue adapter (a recording-fake in tests; never a live venue in E6-T1).
        signer: Injected provider-neutral signing control plane (Mode-A fake offline).
        sources: Injected quote source; raises :class:`StaleVenueBook` when gapped/disconnected.
        now_fn: Injected clock returning integer SECONDS (used for the staleness gate and, x1000,
            for every event's integer-millisecond ``recv_ts``).
        sleep_fn: Injected async delay seam (unused in this single-pass skeleton; wired later).
        envelope: Policy envelope providing ``max_quote_age_s`` and the venue allowlist.
        manifest: Pinned strategy manifest providing the token ``universe`` to quote.
        mode: Execution mode — ``"dry_run"`` (Mode A, no orders) or ``"live_guarded"`` (Mode B).

    Returns:
        A :class:`DustExecutionResult` with one :class:`SubmitDecision` per token, the session
        preamble, and the full ordered lifecycle-event stream.
    """
    seqc = _SeqCounter()
    session_meta = _build_session_meta(manifest=manifest, envelope=envelope, signer=signer, mode=mode)

    events: list[LifecycleEvent] = [
        _build_risk_snapshot(seq=seqc.next(), now_ms=now_fn() * 1000, envelope=envelope)
    ]

    decisions: list[SubmitDecision] = []
    for token_id in manifest.universe:
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
        )
        decisions.append(decision)
        events.extend(token_events)

    events.append(_build_label_event(seq=seqc.next(), now_ms=now_fn() * 1000, manifest=manifest))

    return DustExecutionResult(
        mode=mode,
        decisions=tuple(decisions),
        session_meta=session_meta,
        events=tuple(events),
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
) -> tuple[SubmitDecision, tuple[LifecycleEvent, ...]]:
    """Gate one token's quote and, only when clear AND Mode B, submit it on the wire.

    Also builds the E1-T2 per-decision lifecycle events for a GATE-CLEAR quote — identically shaped
    in both modes (AC-003); see :func:`_emit_order_lifecycle`. A gate-ABSTAINED token (the original
    E6-T1 behavior) emits NO per-decision lifecycle events.
    """
    try:
        quote = await sources.read_quote(token_id)
    except StaleVenueBook:
        # A gapped / disconnected / mid-resync source — abstain, nothing reaches the wire.
        return _abstain(token_id, "stale_source"), ()

    now_s = now_fn()
    reason = _evaluate_submit_gate(quote, now_s=now_s, max_quote_age_s=envelope.max_quote_age_s)
    if reason is not None:
        return _abstain(token_id, reason), ()

    decision, events = await _emit_order_lifecycle(
        quote,
        adapter=adapter,
        signer=signer,
        envelope=envelope,
        manifest=manifest,
        mode=mode,
        now_s=now_s,
        seqc=seqc,
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
) -> tuple[SubmitDecision, tuple[LifecycleEvent, ...]]:
    """Build the full E1-T2 per-decision lifecycle chain for a GATE-CLEAR quote (AC-003).

    Emits, in order: ``OrderSubmitIntent -> OrderSubmitAttempt -> OrderAckEvent ->
    OrderStatusEvent -> RealFillReconciliation``. IDENTICAL event TYPES and ORDERING in both
    modes — Mode A signs the SAME payload but NEVER calls ``adapter.submit_order`` (the E6-T1
    ``adapter.submit_calls == 0`` / AC-017 invariant is unchanged); its ``OrderAckEvent`` honestly
    records ``ack_status="dry_run_not_submitted"`` and a ``None`` venue_order_id instead of
    fabricating a real acknowledgement. The status + reconciliation stages report honest
    PROVISIONAL data in BOTH modes — real status polling / reconciliation wiring is E6-T3's job;
    this only proves the stream SHAPE, not a resolved fill.

    PROVISIONAL: price/size are the E6-T1 placeholders (real sizing is E6-T4); the presubmit
    record's ``venue_order_key`` is a placeholder distinct from the private integrity digest — the
    real EIP-712 V2 order-hash binding (``veridex.dust_execution.signing_compiler``) is wired by a
    later task.
    """
    assert quote.bid is not None and quote.ask is not None  # noqa: S101 - gate guaranteed both sides

    now_ms = now_s * 1000
    client_order_id = f"{manifest.strategy_id}:{quote.token_id}"
    decision_id = client_order_id
    source_ts = quote.quote_ts_s

    intent = OrderSubmitIntent(
        sequence_no=seqc.next(),
        event_type="OrderSubmitIntent",
        source_ts=source_ts,
        recv_ts=now_ms,
        token_id=quote.token_id,
        side="BUY",
        price=quote.ask.price,
        size=1.0,  # provisional placeholder — real size binding is E6-T4 (resolve_dust_size)
        tif="FOK",
        client_order_id=client_order_id,
        decision_id=decision_id,
        decision_ts=now_ms,
    )

    payload = SigningPayload(
        token_id=quote.token_id,
        side="BUY",
        native_price=quote.ask.price,
        size=1.0,  # provisional placeholder — real size binding is E6-T4
        tif="FOK",
        tick_size="0.01",
        client_order_id=client_order_id,
    )
    signed = await signer.sign_order(payload)
    presubmit = PreSubmitRecord(
        integrity_commitment_hash=signed.order_digest,
        # PROVISIONAL: a placeholder join key distinct from the private integrity digest — the
        # real EIP-712 V2 order hash (signing_compiler.eip712_digest) is wired by a later task.
        venue_order_key=f"provisional-vok:{signed.order_digest}",
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
    if mode == "live_guarded":
        # Mode B, every gate clear: sign then submit the ONE order the gates protect. Both sides
        # are present here (missing-side would have abstained above), so this is safe.
        order = Order(
            market_ref=manifest.market,
            side="BUY",
            size=1.0,  # provisional placeholder — real size binding is E6-T4
            price=1.0 / quote.ask.price,  # provisional native→decimal — real pricing is E6-T4
            venue=envelope.venue_allowlist[0] if envelope.venue_allowlist else "dust",
            client_order_id=client_order_id,
        )
        ack = await adapter.submit_order(order)
        venue_order_id = ack.venue_order_id
        submitted = True
        ack_event: OrderAckEvent = OrderAckEvent(
            sequence_no=seqc.next(),
            event_type="OrderAckEvent",
            source_ts=source_ts,
            recv_ts=now_ms,
            decision_id=decision_id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            ack_status="accepted" if ack.accepted else "not_accepted",
        )
    else:
        # Mode A (dry_run): the SAME typed ack-stage event, honestly recording that NO wire was
        # touched — AC-003 keeps the contract SHAPE identical while never reaching
        # adapter.submit_order (AC-017 / the E6-T1 invariant).
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

    status_event = OrderStatusEvent(
        sequence_no=seqc.next(),
        event_type="OrderStatusEvent",
        source_ts=source_ts,
        recv_ts=now_ms,
        decision_id=decision_id,
        client_order_id=client_order_id,
        venue_order_id=venue_order_id,
        status="unresolved",  # PROVISIONAL — real status polling is wired by E6-T3 (reconcile)
        filled_size=0.0,
        fill_price=None,
    )
    reconciliation_event = RealFillReconciliation(
        sequence_no=seqc.next(),
        event_type="RealFillReconciliation",
        source_ts=source_ts,
        recv_ts=now_ms,
        decision_id=decision_id,
        venue_order_key=presubmit.venue_order_key,
        reconciled_state="AMBIGUOUS",  # PROVISIONAL — real reconcile wiring is E6-T3
        reconciled_fill_size=0.0,
    )

    decision = SubmitDecision(
        token_id=quote.token_id,
        submitted=submitted,
        abstain_reason=None if submitted else "mode_a_no_orders",
        venue_order_id=venue_order_id,
    )
    events: tuple[LifecycleEvent, ...] = (intent, attempt, ack_event, status_event, reconciliation_event)
    return decision, events


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
    "QuoteSource",
    "StaleVenueBook",
    "SubmitDecision",
    "run_dust_execution",
]
