"""E6-T1 — ``run_dust_execution`` skeleton + submit-gate tests (SAF-007, AC-010/017, §6 group 6).

Trust boundaries proven here (the SAFETY CORE of the dust-execution runner):

* **Mode A places NO orders.** In ``dry_run`` mode a fully clean, gate-passing quote never reaches
  the injected recording-fake adapter's ``submit_order`` wire.
* **Submit gates refuse to submit (abstain, no order on the wire)** when ANY of: the quote age
  exceeds ``envelope.max_quote_age_s``; the injected source is gapped / disconnected / mid-resync
  (raises :class:`~veridex.dust_execution.runner.StaleVenueBook`); the market is event-suspended;
  a no-quote / boundary state; a negative-liquidity book. A **missing book side is ABSTAINED,
  never imputed / fabricated**.
* **No secret leaks** into the decision telemetry — every field is a JSON-primitive / closed-vocab
  reason, never a raw signer artifact, order, or venue handle.

Everything is INJECTED (adapter, signer, source, clocks, envelope, manifest, mode) — the lane's
async discipline: no wall-clock, no real sleep, Mode B stays UNARMED and offline (the adapter is
the established :class:`~veridex.venues.sx_bet.FakeVenueAdapter` recording-fake; the signer is the
Mode-A :class:`~veridex.dust_execution.signer.LocalFakeWalletControlPlane`).

The positive control (``test_mode_b_clear_quote_submits``) proves the wire actually fires when every
gate is clear, so the mutation check (delete the staleness gate → a stale quote submits → the stale
test fails) is meaningful and not vacuously green.
"""

from __future__ import annotations

from veridex.dust_execution.contracts import (
    DustExecutionSessionMeta,
    DustRunLabelEvent,
    ExecutionMode,
    OrderAckEvent,
    OrderStatusEvent,
    OrderSubmitAttempt,
    OrderSubmitIntent,
    RealFillReconciliation,
    SessionRiskSnapshot,
)
from veridex.dust_execution.emergency import DustSafetySession, SafetyController
from veridex.dust_execution.manifest import StrategyExperimentManifest
from veridex.dust_execution.noncrossing import LegKind, OwnOrderLeg
from veridex.dust_execution.risk import RealizedFillRecord, RiskAccumulator
from veridex.dust_execution.runner import (
    ABSTAIN_REASONS,
    BookSide,
    DustExecutionResult,
    DustQuote,
    StaleVenueBook,
    SubmitDecision,
    run_dust_execution,
)
from veridex.dust_execution.signer import LocalFakeWalletControlPlane, SignedArtifact
from veridex.policy.circuit_breaker import CircuitBreaker, CircuitState
from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime.evidence import compute_evidence_hash
from veridex.venues.base import Order
from veridex.venues.sx_bet import FakeVenueAdapter

# The E1-T2 canonical lifecycle-stream ordering (session meta precedes this, unnumbered):
# risk snapshot -> intent -> attempt -> ack/reject -> status -> fill/reconciliation -> labels.
_EXPECTED_EVENT_TYPES: tuple[str, ...] = (
    "SessionRiskSnapshot",
    "OrderSubmitIntent",
    "OrderSubmitAttempt",
    "OrderAckEvent",
    "OrderStatusEvent",
    "RealFillReconciliation",
    "DustRunLabelEvent",
)

# --- Fixtures --------------------------------------------------------------------------------

_NOW_S = 1_700_000_000  # frozen source clock, integer SECONDS (matches max_quote_age_s units)
_TOKEN = "0xtokenYES"


def _manifest(**kw: object) -> StrategyExperimentManifest:
    base: dict[str, object] = {
        "strategy_id": "dust-maker-v0",
        "strategy_config_hash": "cfg" * 4,
        "evidence_class": "EXPERIMENTAL_DUST",
        "market": "0xcondition",
        "universe": (_TOKEN,),
        "mode": "dry_run",
        "max_orders": 3,
        "max_notional": 5.0,
        "max_session_loss": 2.0,
        "max_daily_loss": 4.0,
        "session_window": (1_700_000_000_000, 1_700_000_600_000),
        "required_inputs": ("fair_value", "venue_book"),
        "permitted_intent_kinds": ("make",),
        "market_fee_snapshot_hash": "fee" * 4,
        "operator_authorization": "op-ref-1",
        "forbidden_claims": ("PROVEN_EDGE", "CALIBRATED"),
    }
    base.update(kw)
    return StrategyExperimentManifest(**base)  # type: ignore[arg-type]


def _env(**kw: object) -> PolicyEnvelope:
    base: dict[str, object] = {
        "max_stake": 100.0,
        "max_orders_per_run": 5,
        "max_orders_per_session": 20,
        "max_orders_per_day": 50,
        "venue_allowlist": ["sx_bet"],
        "market_allowlist": ["0xcondition"],
        "min_edge_bps": 50,
        "max_slippage_bps": 100,
        "max_price": 3.0,
        "max_quote_age_s": 10,
        "cooldown_s": 0,
        "human_approval_threshold": 1000.0,
        "kill_switch": False,
    }
    base.update(kw)
    return PolicyEnvelope(**base)  # type: ignore[arg-type]


def _fresh_quote(**kw: object) -> DustQuote:
    """A fully clean, gate-passing quote captured exactly at ``_NOW_S`` (age 0)."""
    base: dict[str, object] = {
        "token_id": _TOKEN,
        "quote_ts_s": _NOW_S,
        "event_suspended": False,
        "no_quote": False,
        "bid": BookSide(price=0.49, size=10.0),
        "ask": BookSide(price=0.51, size=10.0),
    }
    base.update(kw)
    return DustQuote(**base)  # type: ignore[arg-type]


class _ScriptedSource:
    """A recording-free injected quote source: returns a scripted quote OR raises on read.

    ``raises`` models the gapped / disconnected / mid-resync source that refuses to serve a stale
    book (mirrors the live-recorder concept, defined IN-LANE per SEC-003).
    """

    def __init__(self, *, quote: DustQuote | None = None, raises: BaseException | None = None) -> None:
        self._quote = quote
        self._raises = raises
        self.reads: list[str] = []

    async def read_quote(self, token_id: str) -> DustQuote:
        self.reads.append(token_id)
        if self._raises is not None:
            raise self._raises
        assert self._quote is not None
        return self._quote


def _clock() -> int:
    return _NOW_S


async def _noop_sleep(_seconds: float) -> None:  # injected sleep seam — never a real wall-clock wait
    return None


async def _run(*, adapter: FakeVenueAdapter, source: _ScriptedSource, mode: ExecutionMode) -> DustExecutionResult:
    return await run_dust_execution(
        adapter=adapter,
        signer=LocalFakeWalletControlPlane(),
        sources=source,
        now_fn=_clock,
        sleep_fn=_noop_sleep,
        envelope=_env(),
        manifest=_manifest(mode=mode),
        mode=mode,
    )


# --- Mode A: places NO orders ----------------------------------------------------------------


async def test_mode_a_places_no_orders() -> None:
    """A fully clean quote in Mode A (``dry_run``) never reaches the submit wire (AC-017)."""
    adapter = FakeVenueAdapter(fill=True)
    source = _ScriptedSource(quote=_fresh_quote())

    result = await _run(adapter=adapter, source=source, mode="dry_run")

    assert adapter.submit_calls == 0, "Mode A must place NO orders"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "mode_a_no_orders"
    assert decision.venue_order_id is None
    assert result.submitted_count == 0


# --- Positive control: Mode B with a clean quote DOES submit ---------------------------------


async def test_mode_b_clear_quote_submits() -> None:
    """POSITIVE CONTROL: in Mode B a fully clean quote fires the submit wire exactly once.

    This is what makes the staleness MUTATION meaningful: if a gate is deleted, the gated quote
    would follow THIS same path onto the wire.
    """
    adapter = FakeVenueAdapter(fill=True)
    source = _ScriptedSource(quote=_fresh_quote())

    result = await _run(adapter=adapter, source=source, mode="live_guarded")

    assert adapter.submit_calls == 1, "a clean Mode B quote must reach the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is True
    assert decision.abstain_reason is None
    assert decision.venue_order_id is not None
    assert result.submitted_count == 1


# --- Submit gates: each gated quote is ABSTAINED, never on the wire --------------------------


async def test_stale_by_age_quote_not_submitted() -> None:
    """MUTATION TARGET: a quote older than ``max_quote_age_s`` never reaches the wire."""
    adapter = FakeVenueAdapter(fill=True)
    # age = max_quote_age_s + 1 second → strictly stale.
    stale = _fresh_quote(quote_ts_s=_NOW_S - (_env().max_quote_age_s + 1))
    source = _ScriptedSource(quote=stale)

    result = await _run(adapter=adapter, source=source, mode="live_guarded")

    assert adapter.submit_calls == 0, "a stale-by-age quote must NOT reach the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "stale_quote_age"


async def test_stale_source_not_submitted() -> None:
    """A gapped / disconnected / mid-resync source raises StaleVenueBook → abstain, no wire."""
    adapter = FakeVenueAdapter(fill=True)
    source = _ScriptedSource(raises=StaleVenueBook("venue book disconnected / mid-resync"))

    result = await _run(adapter=adapter, source=source, mode="live_guarded")

    assert adapter.submit_calls == 0, "a StaleVenueBook source must NOT reach the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "stale_source"


async def test_event_suspended_not_submitted() -> None:
    """An event-suspended market never reaches the submit wire."""
    adapter = FakeVenueAdapter(fill=True)
    source = _ScriptedSource(quote=_fresh_quote(event_suspended=True))

    result = await _run(adapter=adapter, source=source, mode="live_guarded")

    assert adapter.submit_calls == 0, "an event-suspended market must NOT reach the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "event_suspended"


async def test_no_quote_boundary_not_submitted() -> None:
    """An explicit no-quote / boundary state never reaches the submit wire."""
    adapter = FakeVenueAdapter(fill=True)
    source = _ScriptedSource(quote=_fresh_quote(no_quote=True, bid=None, ask=None))

    result = await _run(adapter=adapter, source=source, mode="live_guarded")

    assert adapter.submit_calls == 0, "a no-quote / boundary state must NOT reach the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "no_quote"


async def test_negative_liquidity_not_submitted() -> None:
    """A negative-liquidity book never reaches the submit wire."""
    adapter = FakeVenueAdapter(fill=True)
    source = _ScriptedSource(quote=_fresh_quote(bid=BookSide(price=0.49, size=-1.0)))

    result = await _run(adapter=adapter, source=source, mode="live_guarded")

    assert adapter.submit_calls == 0, "a negative-liquidity book must NOT reach the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "negative_liquidity"


async def test_missing_book_side_abstained_never_imputed() -> None:
    """A missing book side is ABSTAINED and NEVER fabricated/imputed onto the wire."""
    adapter = FakeVenueAdapter(fill=True)
    source = _ScriptedSource(quote=_fresh_quote(ask=None))  # bid present, ask MISSING

    result = await _run(adapter=adapter, source=source, mode="live_guarded")

    assert adapter.submit_calls == 0, "a missing book side must NOT be imputed onto the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "missing_book_side"
    assert decision.venue_order_id is None


# --- No secret / raw-handle leak in the decision telemetry ----------------------------------


async def test_no_raw_handle_or_secret_in_result_telemetry() -> None:
    """Decision telemetry carries only JSON-primitives + closed-vocab reasons — no raw handles."""
    adapter = FakeVenueAdapter(fill=True)
    source = _ScriptedSource(quote=_fresh_quote())

    result = await _run(adapter=adapter, source=source, mode="live_guarded")

    (decision,) = result.decisions
    assert isinstance(decision, SubmitDecision)
    # Every field is a JSON-primitive; no Order / SignedArtifact / adapter handle leaks across.
    for value in vars(decision).values():
        assert value is None or isinstance(value, (str, bool))
        assert not isinstance(value, (Order, SignedArtifact, FakeVenueAdapter))
    # Abstain reasons are drawn from the single closed vocabulary (boolean/id-only telemetry).
    for d in result.decisions:
        assert d.abstain_reason is None or d.abstain_reason in ABSTAIN_REASONS
    assert "mode_a_no_orders" in ABSTAIN_REASONS and "stale_quote_age" in ABSTAIN_REASONS


# --- E6-T2: full lifecycle-event stream, identical contract shape in Mode A and Mode B --------


async def test_mode_a_and_mode_b_emit_identical_lifecycle_contract_shape() -> None:
    """AC-003: the SAME pinned clean quote yields IDENTICAL event TYPES + ORDERING in both modes.

    The stream is: session meta (unnumbered) -> risk snapshot -> intent -> attempt -> ack ->
    status -> fill/reconciliation -> labels. Only the recorded DATA differs (whether a real order
    moved) -- never the shape.
    """
    quote = _fresh_quote()

    result_a = await _run(
        adapter=FakeVenueAdapter(fill=True), source=_ScriptedSource(quote=quote), mode="dry_run"
    )
    result_b = await _run(
        adapter=FakeVenueAdapter(fill=True), source=_ScriptedSource(quote=quote), mode="live_guarded"
    )

    assert isinstance(result_a.session_meta, DustExecutionSessionMeta)
    assert isinstance(result_b.session_meta, DustExecutionSessionMeta)
    assert result_a.session_meta.mode == "dry_run"
    assert result_b.session_meta.mode == "live_guarded"

    types_a = tuple(type(e).__name__ for e in result_a.events)
    types_b = tuple(type(e).__name__ for e in result_b.events)
    assert types_a == _EXPECTED_EVENT_TYPES, f"Mode A stream shape drifted: {types_a}"
    assert types_b == _EXPECTED_EVENT_TYPES, f"Mode B stream shape drifted: {types_b}"
    assert types_a == types_b, "Mode A and Mode B must emit the IDENTICAL event-type stream (AC-003)"

    # The ONLY difference is whether a real order moved -- not the shape of the contracts.
    ack_a = next(e for e in result_a.events if isinstance(e, OrderAckEvent))
    ack_b = next(e for e in result_b.events if isinstance(e, OrderAckEvent))
    assert ack_a.venue_order_id is None, "Mode A must never fabricate a venue_order_id"
    assert ack_b.venue_order_id is not None, "Mode B's clean-gate ack must carry a real venue_order_id"
    assert ack_a.ack_status != ack_b.ack_status

    status_a = next(e for e in result_a.events if isinstance(e, OrderStatusEvent))
    status_b = next(e for e in result_b.events if isinstance(e, OrderStatusEvent))
    assert status_a.status == status_b.status  # same honest provisional status label, both modes

    assert quote.ask is not None
    intent_a = next(e for e in result_a.events if isinstance(e, OrderSubmitIntent))
    intent_b = next(e for e in result_b.events if isinstance(e, OrderSubmitIntent))
    assert intent_a.token_id == intent_b.token_id == _TOKEN
    assert intent_a.price == intent_b.price == quote.ask.price

    attempt_a = next(e for e in result_a.events if isinstance(e, OrderSubmitAttempt))
    attempt_b = next(e for e in result_b.events if isinstance(e, OrderSubmitAttempt))
    assert attempt_a.presubmit_record.integrity_commitment_hash
    assert attempt_b.presubmit_record.integrity_commitment_hash

    recon_a = next(e for e in result_a.events if isinstance(e, RealFillReconciliation))
    recon_b = next(e for e in result_b.events if isinstance(e, RealFillReconciliation))
    assert recon_a.reconciled_state == recon_b.reconciled_state

    labels_a = next(e for e in result_a.events if isinstance(e, DustRunLabelEvent))
    labels_b = next(e for e in result_b.events if isinstance(e, DustRunLabelEvent))
    assert labels_a.run_label == labels_b.run_label == "DUST_LIVE"
    assert labels_a.calibration_label == labels_b.calibration_label == "UNCALIBRATED"
    assert labels_a.edge_label == labels_b.edge_label == "NOT_PROVEN_EDGE"


async def test_sequence_no_unique_append_only_monotonic() -> None:
    """``sequence_no`` is append-only, unique, and gap-free across the whole event stream."""
    result = await _run(
        adapter=FakeVenueAdapter(fill=True), source=_ScriptedSource(quote=_fresh_quote()), mode="live_guarded"
    )

    seqs = [e.sequence_no for e in result.events]
    assert len(seqs) >= len(_EXPECTED_EVENT_TYPES)
    assert seqs == list(range(1, len(seqs) + 1)), "sequence_no must be append-only, unique, and gap-free"

    # The shared canonical evidence-hash helper independently rejects a duplicate sequence_no.
    compute_evidence_hash([e.model_dump() for e in result.events])


# --- E6-T3: runner delegates breaker/loss/kill to SafetyController + non-crossing + reconcile ---
#
# Anti-inert discipline (Codex-M3 / Fable-m2): the RED assertion is on the WIRE — the recording-fake
# adapter's ``cancel_all_orders`` was ACTUALLY awaited, subsequent submits are BLOCKED — NOT that the
# SafetyController is internally correct. A controller that is standalone-correct but that the runner
# never CALLS must make ``test_runner_delegates_breaker_loss_kill_to_safety_controller`` RED.


class RecordingFakeAdapter(FakeVenueAdapter):
    """The established :class:`FakeVenueAdapter` extended to RECORD the cancel-all WIRE call.

    Inherits the sealed four-method :class:`~veridex.venues.base.VenueAdapter` behaviour (submit /
    status / cancel / quote) unchanged and ADDS the two seams E6-T3 wires:

    * ``cancel_all_orders`` — the E2-T3 :class:`~veridex.dust_execution.emergency.CancelAllAdapter`
      sweep wire. ``cancel_all_calls`` increments ONLY when the coroutine is actually awaited, so a
      mere submit-block flag flip inside the controller can never move it (that is the load-bearing
      recording-fake rule: prove the venue sweep FIRED, not that a boolean was set).
    * ``get_fill_history`` — the E4 :class:`~veridex.venues.base.VenueReconciliationReads` surface
      the tri-state reconcile queries by ``venue_order_key``. When ``fill_history_matches`` it echoes
      a matching own trade so the reconcile resolves to ``RESOLVED``; otherwise it stays empty (the
      fail-closed AMBIGUOUS default), so a run that never submits can never fabricate a fill.
    """

    def __init__(self, *, fill: bool = True, fill_history_matches: bool = False) -> None:
        super().__init__(fill=fill)
        self.cancel_all_calls = 0
        self._fill_history_matches = fill_history_matches

    async def cancel_all_orders(self) -> int:
        self.cancel_all_calls += 1
        return 3

    async def get_fill_history(self, **kwargs: object) -> list[dict[str, object]]:
        key = kwargs.get("venue_order_key")
        if not self._fill_history_matches or not isinstance(key, str):
            return []
        # A matched own trade keyed on the OFFICIAL venue_order_key (never Veridex's private digest).
        return [{"taker_order_id": key, "size": 1.0}]


_SESSION_ID = "dust-maker-v0:live_guarded"


def _make_safety() -> tuple[SafetyController, DustSafetySession]:
    return SafetyController(clock_ms=lambda: _NOW_S * 1000), DustSafetySession(session_id=_SESSION_ID)


async def _run_guarded(
    *,
    adapter: FakeVenueAdapter,
    safety: SafetyController | None = None,
    session: DustSafetySession | None = None,
    risk: RiskAccumulator | None = None,
    breaker: CircuitBreaker | None = None,
    realized_fills: tuple[RealizedFillRecord, ...] = (),
    own_legs: tuple[OwnOrderLeg, ...] = (),
    envelope: PolicyEnvelope | None = None,
    source: _ScriptedSource | None = None,
) -> DustExecutionResult:
    return await run_dust_execution(
        adapter=adapter,
        signer=LocalFakeWalletControlPlane(),
        sources=source if source is not None else _ScriptedSource(quote=_fresh_quote()),
        now_fn=_clock,
        sleep_fn=_noop_sleep,
        envelope=envelope if envelope is not None else _env(),
        manifest=_manifest(mode="live_guarded"),
        mode="live_guarded",
        safety=safety,
        session=session,
        risk=risk,
        breaker=breaker,
        realized_fills=realized_fills,
        own_legs=own_legs,
    )


async def test_runner_delegates_breaker_loss_kill_to_safety_controller() -> None:
    """LOAD-BEARING anti-inert: each runner-reachable trigger reaches the SafetyController WIRE.

    Three sub-cases — (a) breaker-open, (b) realized-loss-cap breach via a REAL fill, (c) kill-switch
    engage. For EACH: the runner delegates to the E2-T3 :class:`SafetyController`, the recording-fake
    ``cancel_all_orders`` WIRE is ACTUALLY fired, subsequent submits are BLOCKED (no order reaches the
    submit wire), and the ack carries the honest trigger CAUSE, never an order id.
    """
    # (a) BREAKER-OPEN — an OPEN circuit breaker surfaced to the runner.
    adapter = RecordingFakeAdapter(fill=True)
    safety, session = _make_safety()
    breaker = CircuitBreaker(state=CircuitState.OPEN, opened_at=0.0, consecutive_failures=5)

    result = await _run_guarded(adapter=adapter, safety=safety, session=session, breaker=breaker)

    assert adapter.cancel_all_calls == 1, "breaker-open must fire the recording-fake cancel-all WIRE"
    assert session.submit_blocked is True
    assert safety.check_can_submit(session) is False
    assert adapter.submit_calls == 0, "a swept session must place NO further orders on the wire"
    (decision,) = result.decisions
    assert decision.submitted is False and decision.abstain_reason == "safety_blocked"
    assert session.last_cancel_all_ack is not None
    assert session.last_cancel_all_ack.trigger_cause == "breaker"
    assert "venue_order_id" not in session.last_cancel_all_ack.model_dump()

    # (b) REALIZED-LOSS-CAP BREACH — driven by a REAL fill through the RiskAccumulator.
    adapter = RecordingFakeAdapter(fill=True)
    safety, session = _make_safety()
    risk = RiskAccumulator(_SESSION_ID)
    loss_fill = RealizedFillRecord(
        realized_pnl=-2.5, fee=0.0, session_id=_SESSION_ID, fill_ts_ms=_NOW_S * 1000
    )
    env = _env(max_session_loss=2.0, max_daily_loss=4.0)

    result = await _run_guarded(
        adapter=adapter,
        safety=safety,
        session=session,
        risk=risk,
        realized_fills=(loss_fill,),
        envelope=env,
    )

    assert adapter.cancel_all_calls == 1, "a realized-loss breach must fire the cancel-all WIRE"
    assert session.submit_blocked is True
    assert adapter.submit_calls == 0
    (decision,) = result.decisions
    assert decision.submitted is False and decision.abstain_reason == "safety_blocked"
    assert session.last_cancel_all_ack is not None
    assert session.last_cancel_all_ack.trigger_cause == "loss_breach"
    assert "venue_order_id" not in session.last_cancel_all_ack.model_dump()

    # (c) KILL-SWITCH ENGAGE — envelope.kill_switch surfaced to the runner.
    adapter = RecordingFakeAdapter(fill=True)
    safety, session = _make_safety()
    env = _env(kill_switch=True)

    result = await _run_guarded(adapter=adapter, safety=safety, session=session, envelope=env)

    assert adapter.cancel_all_calls == 1, "kill-switch engage must fire the cancel-all WIRE"
    assert session.submit_blocked is True
    assert adapter.submit_calls == 0
    (decision,) = result.decisions
    assert decision.submitted is False and decision.abstain_reason == "safety_blocked"
    assert session.last_cancel_all_ack is not None
    assert session.last_cancel_all_ack.trigger_cause == "kill_switch"
    assert "venue_order_id" not in session.last_cancel_all_ack.model_dump()


async def test_crossing_order_refused_in_submit_path() -> None:
    """MUTATION TARGET (non-crossing): a proposed order that self-crosses an own leg NEVER submits.

    An own resting SELL (ask) at 0.50 on the SAME token, with the proposed BUY at the quote's ask
    (0.51), self-crosses (``highest_own_bid 0.51 >= lowest_own_ask 0.50``). The runner MUST route the
    proposed order through :func:`~veridex.dust_execution.noncrossing.check_non_crossing` BEFORE the
    submit wire and REFUSE it. Bypassing that call lets the crossing order reach ``submit_order``.
    """
    adapter = RecordingFakeAdapter(fill=True)
    safety, session = _make_safety()
    own = (OwnOrderLeg(token_id=_TOKEN, side="SELL", price=0.50, kind=LegKind.OPEN),)

    result = await _run_guarded(adapter=adapter, safety=safety, session=session, own_legs=own)

    assert adapter.submit_calls == 0, "a self-crossing proposed order must NOT reach the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "self_cross"


async def test_non_crossing_clear_order_still_submits() -> None:
    """POSITIVE CONTROL for the non-crossing gate: a NON-crossing own leg still lets the order submit.

    Makes the crossing MUTATION meaningful: an own SELL at 0.80 (well above the proposed BUY 0.51) does
    NOT cross, so the clean order still reaches the wire exactly once.
    """
    adapter = RecordingFakeAdapter(fill=True)
    safety, session = _make_safety()
    own = (OwnOrderLeg(token_id=_TOKEN, side="SELL", price=0.80, kind=LegKind.OPEN),)

    result = await _run_guarded(adapter=adapter, safety=safety, session=session, own_legs=own)

    assert adapter.submit_calls == 1
    (decision,) = result.decisions
    assert decision.submitted is True and decision.abstain_reason is None


async def test_runner_wires_real_reconcile_resolved_status() -> None:
    """The E6-T2 PROVISIONAL status/reconcile seam is CLOSED: it reflects recording-fake venue truth.

    Mode B submits, then the runner routes the presubmit through the E4 tri-state reconcile
    (:func:`~veridex.dust_execution.reconcile.assess_uncertain_submit`) keyed on the ``venue_order_key``.
    The recording-fake echoes a matching own fill, so the status resolves to ``filled`` and the
    reconciliation to ``RESOLVED`` with the matched size — never the hardcoded ``unresolved`` /
    ``AMBIGUOUS`` placeholders.
    """
    adapter = RecordingFakeAdapter(fill=True, fill_history_matches=True)
    safety, session = _make_safety()

    result = await _run_guarded(adapter=adapter, safety=safety, session=session)

    assert adapter.submit_calls == 1
    status = next(e for e in result.events if isinstance(e, OrderStatusEvent))
    recon = next(e for e in result.events if isinstance(e, RealFillReconciliation))
    assert status.status == "filled", "reconcile against venue truth must resolve the honest status"
    assert status.filled_size == 1.0
    assert recon.reconciled_state == "RESOLVED"
    assert recon.reconciled_fill_size == 1.0


async def test_runner_risk_snapshot_threads_real_realized_loss() -> None:
    """The E6-T2 PROVISIONAL risk seam is CLOSED: the snapshot carries the RiskAccumulator's real loss.

    A REAL fill (fee-inclusive loss 1.25) that does NOT breach the (disabled) caps is folded through
    the accumulator; the ``SessionRiskSnapshot`` reports the real ``realized_loss_session/daily`` (1.25)
    instead of the hardcoded 0.0 placeholder, and the run still proceeds (no sweep).
    """
    adapter = RecordingFakeAdapter(fill=True)
    safety, session = _make_safety()
    risk = RiskAccumulator(_SESSION_ID)
    fill = RealizedFillRecord(
        realized_pnl=-1.0, fee=0.25, session_id=_SESSION_ID, fill_ts_ms=_NOW_S * 1000
    )

    result = await _run_guarded(
        adapter=adapter, safety=safety, session=session, risk=risk, realized_fills=(fill,)
    )

    snap = next(e for e in result.events if isinstance(e, SessionRiskSnapshot))
    assert snap.realized_loss_session == 1.25
    assert snap.realized_loss_daily == 1.25
    assert snap.breaker_open is False
    assert snap.kill_switch_engaged is False
    assert adapter.cancel_all_calls == 0, "a non-breaching fill must NOT fire the cancel-all wire"
    assert adapter.submit_calls == 1, "a non-breaching fill leaves the submit path open"
