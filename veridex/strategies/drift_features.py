"""II-7 — the ONE shared, pure drift-feature projector for the arena's drift contestants.

The cumulative-drift feature math (first/current logit, cumulative logit drift, EWMA-slope trend
strength, tick count, observation horizon) was originally inline in
:meth:`~veridex.strategies.drift.CumulativeDriftStrategy._score_side`. This module lifts that math —
VERBATIM (same operations, same order, no rounding introduced) — into a single pure function so BOTH
the deterministic drift agent and the LLM-Drift contestant (II-8) decide from an *identical*
:class:`DriftFeatureSnapshot`. That shared snapshot is the whole point: same features in ⇒ the two
agents differ only in their decision logic, never in what they saw.

PURE by construction: :func:`drift_features` is a total function of ``(logit_series, ts_first, ts,
params)`` — no decision state, no I/O, no side effects, no mutation of its arguments. The GATES and
the firing decision (enough ticks, enough horizon, RISING, trend threshold, cooldown) stay in
``CumulativeDriftStrategy``; only the feature PROJECTION lives here.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from veridex.runtime.evidence import serialize_payload
from veridex.strategies.sharp_stats import ewma

# Codebase-default tradeable band (mirrors ``DEFAULT_MARKET_QUALITY_CONFIG`` in
# :mod:`veridex.strategies.market_quality`): outside it the current implied probability is degenerate
# / near-certain. This is a CONTEXT signal for consumers; the deterministic drift gates never read it.
_DEFAULT_BAND_LO = 0.05
_DEFAULT_BAND_HI = 0.95


@dataclass(frozen=True)
class DriftFeatureParams:
    """The parameters the FEATURE projection needs (distinct from the strategy's DECISION thresholds).

    Only ``ewma_slope_alpha`` affects the feature math extracted from ``_score_side``; the band bounds
    classify the ``market_quality`` context signal and default to the codebase's tradeable band.
    """

    ewma_slope_alpha: float
    quality_band_lo: float = _DEFAULT_BAND_LO
    quality_band_hi: float = _DEFAULT_BAND_HI


@dataclass(frozen=True)
class DriftFeatureSnapshot:
    """The immutable per-side drift feature view — the stable contract II-8 (LLM-Drift) consumes.

    Fields:
        first: Logit of the side's first observed probability (``logit_series[0]``).
        current: Logit of the side's latest observed probability (``logit_series[-1]``).
        cum_logit_drift: Cumulative logit drift ``current - first`` (RISING when positive).
        ewma_slope: EWMA of the per-tick drift DIRECTION (+1 up / -1 down / 0 flat), smoothing
            factor ``params.ewma_slope_alpha``. A smooth monotone rise ⇒ ``+1``; choppy noise ⇒ ~0.
            ``0.0`` when there are fewer than two ticks (no per-tick direction exists yet).
        trend_strength: The EWMA-slope trend strength the decision gate reads — identical to
            ``ewma_slope`` (the deterministic drift agent gates on the slope directly). Exposed under
            its own role-name for downstream consumers.
        tick_count: Number of observations in the window (``len(logit_series)``).
        horizon_s: Observation horizon in seconds (``ts - ts_first``).
        market_quality: ``True`` when the current implied probability sits inside the tradeable band
            (not near-certain). A market-context signal; the deterministic gates never read it.
        evidence_hash: Deterministic SHA-256 content hash over the snapshot's defining inputs/fields
            (same ``(logit_series, ts_first, ts, params)`` ⇒ same hash; a changed window ⇒ a new hash).
    """

    first: float
    current: float
    cum_logit_drift: float
    ewma_slope: float
    trend_strength: float
    tick_count: int
    horizon_s: int
    market_quality: bool
    evidence_hash: str


def _direction(delta: float) -> float:
    """Sign of a per-tick logit move: ``+1`` up, ``-1`` down, ``0`` flat (the drift-trend indicator)."""
    if delta > 0.0:
        return 1.0
    if delta < 0.0:
        return -1.0
    return 0.0


def drift_features(
    logit_series: list[float],
    ts_first: int,
    ts: int,
    params: DriftFeatureParams,
) -> DriftFeatureSnapshot:
    """Project a side's logit series into its :class:`DriftFeatureSnapshot` (pure; no decision state).

    The feature math is the exact computation the pre-refactor ``_score_side`` performed inline:
    ``cum_logit_drift = series[-1] - series[0]`` and ``ewma_slope = ewma(per-tick directions, alpha)``.
    No rounding is introduced. The GATES that read these features stay in ``CumulativeDriftStrategy``.

    Args:
        logit_series: The side's observed logits, oldest first (non-empty).
        ts_first: Timestamp (seconds) of the side's first observation.
        ts: Timestamp (seconds) of the latest observation.
        params: Feature-projection params (EWMA alpha + market-quality band).

    Returns:
        The immutable feature snapshot for this window.

    Raises:
        ValueError: If ``logit_series`` is empty.
    """
    if not logit_series:
        raise ValueError("drift_features requires at least one logit observation")

    first = logit_series[0]
    current = logit_series[-1]
    cum_logit_drift = current - first
    tick_count = len(logit_series)
    horizon_s = ts - ts_first

    # EWMA-slope trend strength: EWMA of the per-tick drift DIRECTION (verbatim from _score_side).
    # With fewer than two ticks there is no per-tick direction yet — the strategy abstains before
    # reading it, so 0.0 is a safe, decision-neutral default that keeps the projector total.
    if tick_count >= 2:
        directions = [_direction(logit_series[i] - logit_series[i - 1]) for i in range(1, tick_count)]
        ewma_slope = ewma(directions, params.ewma_slope_alpha)
    else:
        ewma_slope = 0.0
    trend_strength = ewma_slope

    current_prob = 1.0 / (1.0 + math.exp(-current))
    market_quality = params.quality_band_lo <= current_prob <= params.quality_band_hi

    payload = {
        "logit_series": list(logit_series),
        "ts_first": ts_first,
        "ts": ts,
        "ewma_slope_alpha": params.ewma_slope_alpha,
        "quality_band_lo": params.quality_band_lo,
        "quality_band_hi": params.quality_band_hi,
        "first": first,
        "current": current,
        "cum_logit_drift": cum_logit_drift,
        "ewma_slope": ewma_slope,
        "trend_strength": trend_strength,
        "tick_count": tick_count,
        "horizon_s": horizon_s,
        "market_quality": market_quality,
    }
    evidence_hash = hashlib.sha256(serialize_payload(payload).encode("utf-8")).hexdigest()

    return DriftFeatureSnapshot(
        first=first,
        current=current,
        cum_logit_drift=cum_logit_drift,
        ewma_slope=ewma_slope,
        trend_strength=trend_strength,
        tick_count=tick_count,
        horizon_s=horizon_s,
        market_quality=market_quality,
        evidence_hash=evidence_hash,
    )
