"""E3 event-window computation for the lag-vs-overreaction fork probe.

Given a scoring-side in-running 1X2 fair-probability series (``TrackedTick``, E2)
and a goal event (``GoalEvent``, E1), this module computes the per-event record
the aggregator (E4) consumes:

* ``P_pre``  -- last valid tick in ``[t_e - pre_window_s, t_e)`` (CON-008).
* ``P_imm``  -- FIRST valid tick in ``[t_e, t_e + imm_max_s]`` (CON-004); a tick
  beyond the cap is never treated as "immediate".
* ``P_settle`` -- nearest valid tick within ``±settle_tol_s`` of the primary
  horizon ``t_e + primary_horizon_s`` (CON-003), via ``pick_tick`` -- no
  interpolation, no averaging.

Moves are measured in **logit** units (CON-002) so small-probability moves do not
explode into artifacts, and the raw ``delta_imm``, raw ``delta_settle``, and the
classifier ratio ``R = delta_imm / delta_settle`` are ALL reported for an eligible
event (GUD-001). Classification (CON-005/006):

* ``R in (0, 1)`` -> ``LAG``      (market kept moving -> follow)
* ``R > 1``       -> ``OVERSHOOT`` (immediate overshot the settle, then reverted)
* ``R < 0``       -> ``REVERSAL``  (immediate opposite to settle; kept distinct)
* ``|delta_settle| < epsilon`` -> ``NO-SIGNAL``

Events failing the observability floor (CON-008) are ``NO-SIGNAL`` with a named
``exclusion_reason`` and are counted, never silently dropped.

Thresholds are read from ``WindowConfig`` (this task's local config); E5's
``ProbeConfig`` becomes the sealed superset -- no threshold literal is inlined in
the logic below.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from veridex.backtest.event_probe.extraction import GoalEvent
from veridex.backtest.event_probe.series import TrackedTick


@dataclass(frozen=True)
class WindowConfig:
    """E3-local threshold config (CON-002..006/008 defaults).

    E5's ``ProbeConfig`` is the sealed superset / single source of truth; all
    thresholds are read from this object -- no literals inlined in the logic.

    Precondition: ``epsilon`` must be ``> 0``. The pinned default ``0.05`` (CON-006)
    satisfies this; ``epsilon == 0`` would disable the NO-SIGNAL band (the strict
    ``|delta_settle| < epsilon`` guard could never fire) and is out of spec.
    """

    pre_window_s: int = 120
    imm_max_s: int = 60
    primary_horizon_s: int = 300
    settle_tol_s: int = 30
    epsilon: float = 0.05
    robustness_horizons_s: tuple[int, ...] = (30, 60, 600)
    #: Per-event observability floor (CON-008): minimum valid odds states across
    #: ``[t_e - pre_window_s, t_e + primary_horizon_s]`` for an event to be eligible.
    min_odds_states: int = 3


@dataclass(frozen=True)
class EventRecord:
    """One goal event's window computation + classification (the E4 input)."""

    t_e: int
    scoring_side: str
    participant: int
    p_pre: float | None
    p_imm: float | None
    p_settle: float | None
    delta_imm: float | None
    delta_settle: float | None
    R: float | None
    event_class: str
    exclusion_reason: str | None
    grid: dict[int, float | None] = field(default_factory=dict)
    slice_tags: dict[str, str] = field(default_factory=dict)


def to_logit(p: float) -> float:
    """Return ``ln(p / (1 - p))`` -- the logit transform (CON-002).

    Precondition: ``p`` must be in the open interval ``(0, 1)``. This is
    guaranteed upstream by E2's near-certain band guard -- ``build_tracked_series``
    only emits ticks with ``prob`` inside ``[band_lo, band_hi] = [0.05, 0.95]`` --
    so ``0`` / ``1`` never reach here and no explicit guard is added.
    """
    return math.log(p / (1 - p))


def pick_tick(series: list[TrackedTick], target_ts: int, tolerance_s: int) -> float | None:
    """Return the prob of the tick nearest ``target_ts`` within ``±tolerance_s``.

    Nearest by ``|ts - target_ts|``; ``None`` when no tick lands inside the
    tolerance. This is a strict pick -- never an interpolation or an average
    (PAT-002 window-CLV semantics). Ties keep the first-seen nearest tick.
    """
    best_prob: float | None = None
    best_dist: int | None = None
    for tick in series:
        dist = abs(tick.ts - target_ts)
        if dist <= tolerance_s and (best_dist is None or dist < best_dist):
            best_prob = tick.prob
            best_dist = dist
    return best_prob


def _pick_pre(series: list[TrackedTick], t_e: int, pre_window_s: int) -> float | None:
    """Return the LAST tick prob in ``[t_e - pre_window_s, t_e)`` (CON-008)."""
    lo = t_e - pre_window_s
    candidates = [tick for tick in series if lo <= tick.ts < t_e]
    if not candidates:
        return None
    return max(candidates, key=lambda tick: tick.ts).prob


def _pick_imm(series: list[TrackedTick], t_e: int, imm_max_s: int) -> float | None:
    """Return the FIRST tick prob in ``[t_e, t_e + imm_max_s]`` (CON-004)."""
    hi = t_e + imm_max_s
    candidates = [tick for tick in series if t_e <= tick.ts <= hi]
    if not candidates:
        return None
    return min(candidates, key=lambda tick: tick.ts).prob


def classify_reaction(delta_imm: float, delta_settle: float, epsilon: float) -> str:
    """Classify the reaction on the primary horizon (CON-005/006).

    ``|delta_settle| < epsilon`` -> ``NO-SIGNAL``; otherwise ``R < 0`` ->
    ``REVERSAL`` (kept distinct from OVERSHOOT), ``R > 1`` -> ``OVERSHOOT``,
    and ``R in [0, 1]`` -> ``LAG``.
    """
    if abs(delta_settle) < epsilon:
        return "NO-SIGNAL"
    r = delta_imm / delta_settle
    if r < 0:
        return "REVERSAL"
    if r > 1:
        return "OVERSHOOT"
    return "LAG"


def compute_event_record(
    series: list[TrackedTick], event: GoalEvent, cfg: WindowConfig
) -> EventRecord:
    """Compute the per-event record for ``event`` against ``series`` (CON-008)."""
    t_e = event.t_e
    grid_horizons = tuple(sorted(set(cfg.robustness_horizons_s) | {cfg.primary_horizon_s}))

    p_pre = _pick_pre(series, t_e, cfg.pre_window_s)
    p_imm = _pick_imm(series, t_e, cfg.imm_max_s)
    p_settle = pick_tick(series, t_e + cfg.primary_horizon_s, cfg.settle_tol_s)

    def _record(
        *,
        delta_imm: float | None,
        delta_settle: float | None,
        R: float | None,
        event_class: str,
        exclusion_reason: str | None,
        grid: dict[int, float | None],
    ) -> EventRecord:
        return EventRecord(
            t_e=t_e,
            scoring_side=event.scoring_side,
            participant=event.participant,
            p_pre=p_pre,
            p_imm=p_imm,
            p_settle=p_settle,
            delta_imm=delta_imm,
            delta_settle=delta_settle,
            R=R,
            event_class=event_class,
            exclusion_reason=exclusion_reason,
            grid=grid,
            slice_tags={},
        )

    empty_grid: dict[int, float | None] = dict.fromkeys(grid_horizons)

    # Observability floor (CON-008), checked in dependency order so each failure
    # is attributed to its own named reason.
    if p_pre is None:
        return _record(
            delta_imm=None, delta_settle=None, R=None,
            event_class="NO-SIGNAL", exclusion_reason="no_pre_tick", grid=empty_grid,
        )
    if p_imm is None:
        return _record(
            delta_imm=None, delta_settle=None, R=None,
            event_class="NO-SIGNAL", exclusion_reason="no_imm_tick_60s", grid=empty_grid,
        )
    if p_settle is None:
        return _record(
            delta_imm=None, delta_settle=None, R=None,
            event_class="NO-SIGNAL", exclusion_reason="no_settle_tick", grid=empty_grid,
        )

    lo = t_e - cfg.pre_window_s
    hi = t_e + cfg.primary_horizon_s
    n_states = sum(1 for tick in series if lo <= tick.ts <= hi)
    if n_states < cfg.min_odds_states:
        return _record(
            delta_imm=None, delta_settle=None, R=None,
            event_class="NO-SIGNAL", exclusion_reason="insufficient_odds_states",
            grid=empty_grid,
        )

    logit_pre = to_logit(p_pre)
    delta_imm = to_logit(p_imm) - logit_pre
    delta_settle = to_logit(p_settle) - logit_pre

    # Robustness grid: R at each horizon's settle tick (None where that horizon's
    # settle tick is unresolved, or where its settled move is exactly zero).
    grid: dict[int, float | None] = {}
    for h in grid_horizons:
        settle_h = pick_tick(series, t_e + h, cfg.settle_tol_s)
        if settle_h is None:
            grid[h] = None
            continue
        d_settle_h = to_logit(settle_h) - logit_pre
        grid[h] = None if d_settle_h == 0 else delta_imm / d_settle_h

    if abs(delta_settle) < cfg.epsilon:
        return _record(
            delta_imm=delta_imm, delta_settle=delta_settle, R=None,
            event_class="NO-SIGNAL", exclusion_reason="below_epsilon", grid=grid,
        )

    R = delta_imm / delta_settle
    event_class = classify_reaction(delta_imm, delta_settle, cfg.epsilon)
    return _record(
        delta_imm=delta_imm, delta_settle=delta_settle, R=R,
        event_class=event_class, exclusion_reason=None, grid=grid,
    )
