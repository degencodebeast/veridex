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
)
from veridex.live_recorder.executability import measure_take
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
