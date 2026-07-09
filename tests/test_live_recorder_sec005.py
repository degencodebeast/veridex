"""E5-T4 trust-anchor tests (SEC-005 seed) for the live-recorder lane (MM-R3).

A COUNTERFACTUAL :class:`ExecutabilityMeasurement` must NEVER be convertible into a fill,
a realized PnL, or a ranking metric. This asserts the measurement carries NONE of the
directional ``_rank_key`` metric fields the scorer sorts on
(``avg_clv_bps``/``total_clv_bps``/``brier``/``max_drawdown``/``action_count``/``agent_id``
— confirmed in ``veridex/scoring.py``) and NO ``fill_price``/``filled_size``/
``realized_pnl``/``real_executable_edge_bps`` field.

NOTE: E7-T3 finalizes the SEC-006 denylist cross-check on top of this seed; this file is
kept importable + green in the meantime.
"""

from veridex.live_recorder.contracts import ExecutabilityMeasurement

# The directional metric fields the scorer's `_rank_key` sorts a run on
# (veridex/scoring.py:229-238). An executability OBSERVATION must expose none of them.
_RANK_KEY_FIELDS = (
    "avg_clv_bps",
    "total_clv_bps",
    "brier",
    "max_drawdown",
    "action_count",
    "agent_id",
)

# Fill / PnL / realized-value fields that would turn an observation into a claimed fill.
_FILL_PNL_FIELDS = (
    "fill_price",
    "filled_size",
    "realized_pnl",
    "real_executable_edge_bps",
    "spread_capture",
    "fill_rate",
)


def _ex(**kw) -> ExecutabilityMeasurement:
    base = dict(
        candidate_price=0.60, available_size_at_price=8.0, cumulative_size_to_clear=8.0,
        spread=0.02, half_spread=0.01, cost_clearing_threshold=0.60, taker_fee_bps=0,
        fee_stress_multiplier=4, stale_window_s=120, clears=True, label="COUNTERFACTUAL",
    )
    base.update(kw)
    return ExecutabilityMeasurement(**base)


def test_executability_not_convertible_to_fill_or_rank():
    ex = _ex()
    dumped = ex.model_dump()

    # Trust anchor 1: none of the scorer's directional rank-key fields exist on the measurement.
    for field in _RANK_KEY_FIELDS:
        assert not hasattr(ex, field), f"executability must not carry rank-key field {field!r}"
        assert field not in dumped, f"executability dump must not carry rank-key field {field!r}"

    # Trust anchor 2: no fill / PnL / realized-value field exists on the measurement.
    for field in _FILL_PNL_FIELDS:
        assert not hasattr(ex, field), f"executability must not carry fill/PnL field {field!r}"
        assert field not in dumped, f"executability dump must not carry fill/PnL field {field!r}"

    # It IS and stays a COUNTERFACTUAL observation.
    assert ex.label == "COUNTERFACTUAL"
