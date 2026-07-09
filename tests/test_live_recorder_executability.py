"""E5 counterfactual-executability tests for the live-recorder lane (MM-R3).

Trust boundaries under test (the whole point of E5):

* COUNTERFACTUAL only: every :class:`ExecutabilityMeasurement` carries
  ``label="COUNTERFACTUAL"`` — an OBSERVATION of resting depth, never an own fill.
* No fill/PnL/edge: the measurement has NO ``fill_price``/``filled_size``/
  ``realized_pnl``/``real_executable_edge_bps`` field (the E1 model forbids them).
* Queue-jump is DERIVED, not stored: ``outbid_within_ms``/``stepped_ahead_count`` are
  computed from the post-decision book stream into a SEPARATE
  :class:`QueueJumpDerivation`, and are NEVER settable on the immutable ``QuoteIntentEvent``.
* Pinned fee config (incl the Rose 4x stress variant): the bound
  :class:`FillAssumptionConfig` hash is pinned BEFORE measurement; no queue-fill
  probability / queue simulation is ever produced.
"""

import pytest

from veridex.live_recorder.contracts import (
    BookLevel,
    FillAssumptionConfig,
    QuoteIntentEvent,
    VenueBookSnapshotEvent,
)
from veridex.live_recorder.executability import (
    QueueJumpDecision,
    bind_fee_config,
    derive_queue_jump,
    fee_stress_grid,
    measure_take,
    queue_ahead_at,
)
from veridex.live_recorder.sources import BookSnapshot


def _snap(**kw) -> BookSnapshot:
    base = dict(
        token_id="t",
        venue_market_ref="m",
        book_ts=1000,
        tick_size=0.01,
        min_price_increment=0.01,
        bids=(BookLevel(price=0.59, size=10.0),),
        asks=(BookLevel(price=0.60, size=3.0), BookLevel(price=0.61, size=5.0)),
        is_snapshot=True,
    )
    base.update(kw)
    return BookSnapshot(**base)


# --------------------------------------------------------------------------- E5-T1
def test_measure_take_is_counterfactual_never_fill():
    snap = BookSnapshot(
        token_id="t",
        venue_market_ref="m",
        book_ts=1000,
        tick_size=0.01,
        min_price_increment=0.01,
        bids=(BookLevel(price=0.59, size=10.0),),
        asks=(BookLevel(price=0.60, size=3.0), BookLevel(price=0.61, size=5.0)),
        is_snapshot=True,
    )
    cfg = FillAssumptionConfig(
        taker_fee_bps=0, fee_stress_multiplier=1, spread_assumption=0.0, slippage_assumption=0.0
    )
    ex = measure_take(snapshot=snap, candidate_price=0.61, desired_size=5.0, fee_config=cfg)
    assert ex.label == "COUNTERFACTUAL"
    assert not hasattr(ex, "fill_price") and not hasattr(ex, "filled_size")
    # 3 @0.60 + 5 @0.61 available -> clearing 5 is observable; this is an OBSERVATION, not a fill
    assert ex.clears is True


# --------------------------------------------------------------------------- E5-T1 (spec §9)
def test_measure_take_reproduces_spec_worked_example():
    # PINS the spec §9 seq-43 worked example (ground-truth arithmetic).
    snap = BookSnapshot(
        token_id="t", venue_market_ref="m", book_ts=1000, tick_size=0.01, min_price_increment=0.01,
        bids=(BookLevel(price=0.58, size=10.0),),
        asks=(BookLevel(price=0.60, size=8.0),),
        is_snapshot=True,
    )
    cfg = FillAssumptionConfig(
        taker_fee_bps=0, fee_stress_multiplier=4, spread_assumption=0.0, slippage_assumption=0.005
    )
    ex = measure_take(snapshot=snap, candidate_price=0.60, desired_size=5.0, fee_config=cfg)
    assert ex.available_size_at_price == 8.0
    assert ex.cumulative_size_to_clear == 5.0
    assert ex.spread == pytest.approx(0.02)
    assert ex.half_spread == pytest.approx(0.01)
    # candidate 0.60 - half_spread 0.01 - slippage 0.005 - taker_fee 0 = 0.585 (spec §9)
    assert ex.cost_clearing_threshold == pytest.approx(0.585)
    assert ex.clears is True
    assert ex.label == "COUNTERFACTUAL"


def test_measure_take_handles_unsorted_asks():
    # A DESCENDING ask side must not silently under-count: the 3.0 @0.60 (<= 0.61) counts.
    snap = BookSnapshot(
        token_id="t", venue_market_ref="m", book_ts=1000, tick_size=0.01, min_price_increment=0.01,
        bids=(BookLevel(price=0.58, size=10.0),),
        asks=(BookLevel(price=0.62, size=5.0), BookLevel(price=0.60, size=3.0)),
        is_snapshot=True,
    )
    cfg = FillAssumptionConfig(
        taker_fee_bps=0, fee_stress_multiplier=1, spread_assumption=0.0, slippage_assumption=0.0
    )
    ex = measure_take(snapshot=snap, candidate_price=0.61, desired_size=3.0, fee_config=cfg)
    assert ex.cumulative_size_to_clear == 3.0
    assert ex.clears is True


def test_measure_take_abstains_on_empty_ask_side():
    # An empty relevant side EXCLUDES the market (abstain -> None), never clears=False on zero depth.
    snap = BookSnapshot(
        token_id="t", venue_market_ref="m", book_ts=1000, tick_size=0.01, min_price_increment=0.01,
        bids=(BookLevel(price=0.58, size=10.0),),
        asks=(),
        is_snapshot=True,
    )
    cfg = FillAssumptionConfig(
        taker_fee_bps=0, fee_stress_multiplier=1, spread_assumption=0.0, slippage_assumption=0.0
    )
    assert measure_take(snapshot=snap, candidate_price=0.60, desired_size=5.0, fee_config=cfg) is None


# --------------------------------------------------------------------------- E5-T2
def test_queue_jump_is_derived_not_stored():
    # The decision-time intent stores only decision-time fields (e.g. queue_ahead_size).
    qi = QuoteIntentEvent(
        sequence_no=2, event_type="QuoteIntentEvent", source_ts=None, recv_ts=107000,
        decision_id="d-107", native_price=0.60, desired_size=5.0, side="part1",
        ladder_rung=0, quote_intent_type="join", queue_ahead_size=8.0,
    )
    assert qi.queue_ahead_size == 8.0
    # The immutable intent has NO post-decision queue-jump field: constructing one raises.
    with pytest.raises(Exception):
        QuoteIntentEvent(
            sequence_no=2, event_type="QuoteIntentEvent", source_ts=None, recv_ts=107000,
            decision_id="d-107", native_price=0.60, desired_size=5.0, side="part1",
            ladder_rung=0, quote_intent_type="join", queue_ahead_size=8.0, outbid_within_ms=200,
        )

    # Post-decision book stream: someone steps ahead of our 0.60 bid (to 0.61) 200ms later.
    # The recorded book events carry the recorder's OWN recv_ts (ms) — same clock as the
    # decision — while the venue book_ts is a divergent venue-native ms clock (must NOT drive the delta).
    decision = QueueJumpDecision(decision_id="d-107", side="part1", native_price=0.60, recv_ts=107000)
    subsequent = [
        VenueBookSnapshotEvent(  # +100ms: nobody ahead yet (best bid still 0.59)
            sequence_no=10, event_type="VenueBookSnapshotEvent", source_ts=None, recv_ts=107100,
            token_id="t", venue_market_ref="m", book_ts=1710000000100, tick_size=0.01, min_price_increment=0.01,
            bids=(BookLevel(price=0.59, size=10.0),), asks=(BookLevel(price=0.62, size=4.0),), is_snapshot=True,
        ),
        VenueBookSnapshotEvent(  # +200ms: a 0.61 bid steps AHEAD of our 0.60
            sequence_no=11, event_type="VenueBookSnapshotEvent", source_ts=None, recv_ts=107200,
            token_id="t", venue_market_ref="m", book_ts=1710000000200, tick_size=0.01, min_price_increment=0.01,
            bids=(BookLevel(price=0.61, size=6.0), BookLevel(price=0.59, size=10.0)), asks=(BookLevel(price=0.62, size=4.0),), is_snapshot=True,
        ),
        VenueBookSnapshotEvent(  # +300ms: still ahead
            sequence_no=12, event_type="VenueBookSnapshotEvent", source_ts=None, recv_ts=107300,
            token_id="t", venue_market_ref="m", book_ts=1710000000300, tick_size=0.01, min_price_increment=0.01,
            bids=(BookLevel(price=0.61, size=7.0),), asks=(BookLevel(price=0.62, size=4.0),), is_snapshot=True,
        ),
    ]
    d = derive_queue_jump(decision=decision, subsequent_book_events=subsequent)
    assert d.outbid_within_ms == 200            # derived on the SEPARATE analysis object (recv_ts delta)
    assert d.stepped_ahead_count == 2           # two post-decision books showed someone ahead
    assert d.decision_id == "d-107"             # keyed back to the decision, never edits it
    # The derivation is a SEPARATE object, not a field on the intent.
    assert not hasattr(qi, "outbid_within_ms")
    assert not hasattr(qi, "stepped_ahead_count")


def test_queue_jump_uses_recorder_recv_ts_ms():
    # The ms delta MUST key off the recorder's recv_ts (same clock as the decision), NOT the
    # divergent venue book_ts. Here book_ts is a large venue-native ms value; only recv_ts is comparable.
    decision = QueueJumpDecision(decision_id="d-500", side="part1", native_price=0.60, recv_ts=500000)
    events = [
        VenueBookSnapshotEvent(
            sequence_no=1, event_type="VenueBookSnapshotEvent", source_ts=None, recv_ts=500200,
            token_id="t", venue_market_ref="m", book_ts=1710000009999, tick_size=0.01, min_price_increment=0.01,
            bids=(BookLevel(price=0.61, size=6.0),), asks=(BookLevel(price=0.62, size=4.0),), is_snapshot=True,
        ),
    ]
    d = derive_queue_jump(decision=decision, subsequent_book_events=events)
    assert d.outbid_within_ms == 200  # 500200 - 500000, NOT book_ts - recv_ts
    assert d.stepped_ahead_count == 1


def test_queue_ahead_at_is_decision_time_size_never_imputed():
    snap = _snap(bids=(BookLevel(price=0.61, size=4.0), BookLevel(price=0.60, size=8.0), BookLevel(price=0.59, size=2.0)))
    # Resting bid size at prices at least as good as ours (>= 0.60): 4 @0.61 + 8 @0.60 = 12.
    assert queue_ahead_at(snap, side="part1", native_price=0.60) == 12.0
    # An empty book side is honest None (never imputed to 0).
    assert queue_ahead_at(_snap(bids=()), side="part1", native_price=0.60) is None


# --------------------------------------------------------------------------- E5-T3
def test_executability_binds_pinned_fee_config_no_queue_fill_prob():
    cfg = FillAssumptionConfig(
        taker_fee_bps=10, fee_stress_multiplier=1, spread_assumption=0.0, slippage_assumption=0.0
    )
    pinned_hash = cfg.config_hash()  # pinned BEFORE measurement
    snap = _snap()

    # Measurement asserts the passed config's hash equals the pinned hash before running.
    ex = measure_take(
        snapshot=snap, candidate_price=0.61, desired_size=5.0, fee_config=cfg,
        pinned_config_hash=pinned_hash,
    )
    assert ex.label == "COUNTERFACTUAL"

    # A config whose hash was NOT pinned is rejected (no post-hoc edits).
    tampered = FillAssumptionConfig(
        taker_fee_bps=10, fee_stress_multiplier=2, spread_assumption=0.0, slippage_assumption=0.0
    )
    with pytest.raises(Exception):
        measure_take(
            snapshot=snap, candidate_price=0.61, desired_size=5.0, fee_config=tampered,
            pinned_config_hash=pinned_hash,
        )
    with pytest.raises(Exception):
        bind_fee_config(tampered, pinned_hash)

    # No queue-fill probability / queue simulation ANYWHERE in the measurement output.
    for banned in ("fill_probability", "queue_fill", "queue_simulation", "fill_price", "filled_size"):
        assert not hasattr(ex, banned)
    dumped = ex.model_dump()
    for banned in ("fill_probability", "queue_fill", "queue_simulation", "fill_price", "filled_size"):
        assert banned not in dumped

    # Rose 4x hashes DIFFERENTLY from the 1x config (the stress variant is fee_stress_multiplier=4).
    grid = fee_stress_grid(taker_fee_bps=10)
    hashes = {c.fee_stress_multiplier: c.config_hash() for c in grid}
    assert 1.0 in hashes and 4.0 in hashes            # taker-fee-always + Rose 4x both pinned
    assert hashes[1.0] != hashes[4.0]                 # distinct fee-stress dimensions
