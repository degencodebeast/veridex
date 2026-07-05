"""E3 event-window computation tests (CON-002/003/004/005/006/008).

Covers the four pure units in ``compute.py``:

* ``to_logit`` -- the logit transform used so moves are symmetric (CON-002).
* ``pick_tick`` -- nearest-tick lookup within ``±tolerance`` (CON-003); NO
  interpolation, NO averaging.
* ``classify_reaction`` -- the LAG / OVERSHOOT / REVERSAL / NO-SIGNAL classifier
  on the primary horizon (CON-005/006), with REVERSAL kept distinct from
  OVERSHOOT.
* ``compute_event_record`` -- assembles P_pre / P_imm / P_settle window reads,
  raw deltas, R, the robustness grid, and the named exclusion reasons (CON-008).

Series fixtures are hand-built to force one specific branch each so that a test
passing on the correct branch fails on the adjacent one.
"""

from __future__ import annotations

from veridex.backtest.event_probe.compute import (
    WindowConfig,
    classify_reaction,
    compute_event_record,
    pick_tick,
    to_logit,
)
from veridex.backtest.event_probe.extraction import GoalEvent
from veridex.backtest.event_probe.series import TrackedTick


def _event(t_e: int = 1000) -> GoalEvent:
    return GoalEvent(t_e=t_e, scoring_side="home", participant=1)


def test_pick_tick_nearest_within_tolerance() -> None:
    # Ticks at 100, 125, 131. Target 130 -> nearest is 131 (dist 1), returned as
    # that tick's exact prob (no averaging with the 125 or 100 ticks).
    series = [
        TrackedTick(ts=100, prob=0.50),
        TrackedTick(ts=125, prob=0.60),
        TrackedTick(ts=131, prob=0.70),
    ]
    assert pick_tick(series, 130, 30) == 0.70

    # Target 200: nearest tick (131) is 69s away, beyond ±30 -> None.
    assert pick_tick(series, 200, 30) is None

    # Exactly 31s away -> None; exactly 30s away -> included (boundary).
    solo = [TrackedTick(ts=100, prob=0.55)]
    assert pick_tick(solo, 131, 30) is None
    assert pick_tick(solo, 130, 30) == 0.55


def test_classify_lag() -> None:
    # R = 0.29 / 0.65 approx 0.45 in (0, 1) -> LAG.
    assert classify_reaction(0.29, 0.65, 0.05) == "LAG"


def test_classify_overshoot() -> None:
    # R = 1.06 / 0.65 approx 1.63 > 1 -> OVERSHOOT.
    assert classify_reaction(1.06, 0.65, 0.05) == "OVERSHOOT"


def test_classify_reversal_distinct_from_overshoot() -> None:
    # R = -0.20 / 0.65 < 0 -> REVERSAL, explicitly NOT lumped with OVERSHOOT.
    result = classify_reaction(-0.20, 0.65, 0.05)
    assert result == "REVERSAL"
    assert result != "OVERSHOOT"


def test_classify_below_epsilon_no_signal() -> None:
    # |delta_settle| < epsilon -> NO-SIGNAL regardless of delta_imm sign/size.
    assert classify_reaction(1.5, 0.04, 0.05) == "NO-SIGNAL"
    assert classify_reaction(-2.0, 0.02, 0.05) == "NO-SIGNAL"


def test_compute_record_imm_cap_no_signal() -> None:
    # Pre tick present, but the first post-goal tick is at t_e+140 (outside the
    # 60s immediate cap) -> NO-SIGNAL / no_imm_tick_60s (never a stale reading).
    series = [
        TrackedTick(ts=950, prob=0.55),   # pre (t_e-50)
        TrackedTick(ts=1140, prob=0.62),  # first post-goal, t_e+140 > cap
        TrackedTick(ts=1300, prob=0.70),  # settle horizon
    ]
    record = compute_event_record(series, _event(), WindowConfig())
    assert record.event_class == "NO-SIGNAL"
    assert record.exclusion_reason == "no_imm_tick_60s"


def test_compute_record_pre_window() -> None:
    # No tick in [t_e-120, t_e) -> no_pre_tick.
    series = [
        TrackedTick(ts=1030, prob=0.62),  # first tick is at/after t_e
        TrackedTick(ts=1300, prob=0.70),
    ]
    record = compute_event_record(series, _event(), WindowConfig())
    assert record.event_class == "NO-SIGNAL"
    assert record.exclusion_reason == "no_pre_tick"


def test_compute_record_min_odds_states() -> None:
    # Pre (950), imm (1030), and a settle tick at 1320 (within +/-30 of t_e+300)
    # all resolve, but only 2 states fall inside [t_e-120, t_e+300] -> the
    # settle tick sits past the window edge, so the floor of 3 states fails.
    series = [
        TrackedTick(ts=950, prob=0.55),
        TrackedTick(ts=1030, prob=0.62),
        TrackedTick(ts=1320, prob=0.70),  # settle within tol, but ts > t_e+300
    ]
    record = compute_event_record(series, _event(), WindowConfig())
    assert record.p_settle == 0.70  # settle resolved -> not a no_settle_tick case
    assert record.event_class == "NO-SIGNAL"
    assert record.exclusion_reason == "insufficient_odds_states"


def test_compute_record_reports_raw_deltas() -> None:
    # An eligible LAG event: pre 0.55, imm 0.62, settle 0.70 with >=3 states.
    series = [
        TrackedTick(ts=950, prob=0.55),   # pre
        TrackedTick(ts=1030, prob=0.62),  # imm (t_e+30)
        TrackedTick(ts=1150, prob=0.66),  # extra in-window state
        TrackedTick(ts=1300, prob=0.70),  # settle (t_e+300)
    ]
    record = compute_event_record(series, _event(), WindowConfig())

    assert record.exclusion_reason is None
    assert record.event_class == "LAG"
    # GUD-001: raw deltas AND R are all reported for an eligible event.
    assert record.delta_imm is not None
    assert record.delta_settle is not None
    assert record.R is not None
    assert record.delta_imm == to_logit(0.62) - to_logit(0.55)
    assert record.delta_settle == to_logit(0.70) - to_logit(0.55)
    # The robustness grid is populated and the primary horizon resolves.
    assert record.grid[300] is not None
    assert set(record.grid) == {30, 60, 300, 600}
