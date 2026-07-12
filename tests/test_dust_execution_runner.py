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
)
from veridex.dust_execution.manifest import StrategyExperimentManifest
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
