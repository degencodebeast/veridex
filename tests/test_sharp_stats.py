"""T18 — pure sharp-move statistics (REQ-2D-502).

MATH-FIRST TDD. Every fixture array is SEEDED and COMMITTED in this file: there is NO runtime
randomness (no ``np.random``, no wall-clock) anywhere in the decision path or these tests, so the
asserted values are reproducible byte-for-byte.

Covers the four primitives the momentum v2 decision layer is built on:

* :func:`logit` — logit-space probability movement, domain-guarded at the (0, 1) boundary.
* :func:`ewma` — exponential weighted moving average (volatility normalisation).
* :func:`robust_z` — median/MAD robust z-score, immune to a lone outlier that fools mean/std.
* :class:`PageHinkley` — change-point detector: fires on a genuine level shift, quiet on noise.
"""

from __future__ import annotations

import math

import pytest

from veridex.strategies.sharp_stats import PageHinkley, ewma, logit, robust_z

# --------------------------------------------------------------------------------------------
# logit
# --------------------------------------------------------------------------------------------


def test_logit_matches_log_odds() -> None:
    assert logit(0.5) == pytest.approx(0.0)
    assert logit(0.6) == pytest.approx(math.log(0.6 / 0.4))
    assert logit(0.75) == pytest.approx(math.log(3.0))


def test_logit_is_symmetric() -> None:
    # logit(p) == -logit(1 - p) — the defining reflection symmetry of the log-odds.
    for p in (0.6, 0.75, 0.9, 0.123):
        assert logit(p) == pytest.approx(-logit(1.0 - p))


def test_logit_domain_guard_clamps_boundary() -> None:
    # TxLINE probs live in (0, 1); a boundary tick must NOT crash a deterministic backtest.
    # We CLAMP (not raise) with a symmetric epsilon so the value is finite and symmetry holds.
    lo = logit(0.0)
    hi = logit(1.0)
    assert math.isfinite(lo) and math.isfinite(hi)
    assert lo < 0.0 < hi
    assert lo == pytest.approx(-hi)  # symmetric clamp preserves logit(0) == -logit(1)
    # Out-of-range inputs clamp to the same finite bounds (defensive against bad feeds).
    assert logit(-1.0) == pytest.approx(lo)
    assert logit(2.0) == pytest.approx(hi)


# --------------------------------------------------------------------------------------------
# ewma
# --------------------------------------------------------------------------------------------


def test_ewma_of_constant_is_the_constant() -> None:
    assert ewma([5.0, 5.0, 5.0, 5.0], 0.3) == pytest.approx(5.0)
    assert ewma([2.0], 0.5) == pytest.approx(2.0)  # single value → itself


def test_ewma_alpha_one_is_last_value() -> None:
    # alpha = 1 fully discounts history → the EWMA is exactly the latest observation.
    assert ewma([1.0, 2.0, 3.0, 4.0], 1.0) == pytest.approx(4.0)


def test_ewma_small_alpha_reacts_slowly() -> None:
    # A step from 0 to 10 with alpha=0.1 barely moves: s = 0.1*10 + 0.9*0 = 1.0 (far from 10).
    slow = ewma([0.0, 0.0, 0.0, 10.0], 0.1)
    fast = ewma([0.0, 0.0, 0.0, 10.0], 1.0)
    assert slow == pytest.approx(1.0)
    assert fast == pytest.approx(10.0)
    assert slow < fast


def test_ewma_rejects_bad_alpha_and_empty() -> None:
    with pytest.raises(ValueError):
        ewma([1.0, 2.0], 0.0)
    with pytest.raises(ValueError):
        ewma([1.0, 2.0], 1.5)
    with pytest.raises(ValueError):
        ewma([], 0.5)


# --------------------------------------------------------------------------------------------
# robust_z  (median/MAD, scaled by 1.4826)
# --------------------------------------------------------------------------------------------

# A light-tailed (uniform) reference window: for such data std < 1.4826*MAD, so a plain
# (mean/std) z-score is INFLATED relative to the robust one — the setup where plain "fires"
# but robust stays quiet. Committed, deterministic.
_UNIFORM_REF = [-9.0, -7.0, -5.0, -3.0, -1.0, 1.0, 3.0, 5.0, 7.0, 9.0]


def _plain_z_of_latest(series: list[float]) -> float:
    """Naive (mean/std) z-score of the latest point vs its reference — for CONTRAST only."""
    ref = series[:-1]
    mean = sum(ref) / len(ref)
    var = sum((x - mean) ** 2 for x in ref) / len(ref)
    std = math.sqrt(var)
    if std == 0.0:
        return 0.0
    return (series[-1] - mean) / std


def test_robust_z_scores_latest_via_median_mad() -> None:
    # reference median = 0, MAD = median(|x|) = 5 → scale = 1.4826*5 = 7.413.
    series = [*_UNIFORM_REF, 20.0]
    assert robust_z(series) == pytest.approx(20.0 / (1.4826 * 5.0), rel=1e-9)


def test_robust_z_stays_below_threshold_where_plain_z_fires() -> None:
    # SAME latest point: plain z crosses a 3.0 threshold (fires) but robust z stays under it.
    series = [*_UNIFORM_REF, 20.0]
    assert _plain_z_of_latest(series) > 3.0  # plain (mean/std) fires
    assert robust_z(series) < 3.0  # robust (median/MAD) stays quiet


def test_robust_z_is_immune_to_a_single_reference_outlier() -> None:
    # Replacing one reference point with a HUGE outlier corrupts mean/std but leaves the
    # median/MAD verdict UNCHANGED (breakdown point 50%). This is the outlier-immunity property.
    latest = 20.0
    clean = [*_UNIFORM_REF, latest]
    contaminated = [*_UNIFORM_REF[:-1], 500.0, latest]  # last ref point 9 -> 500

    # Robust verdict is byte-identical clean vs contaminated — the outlier is ignored.
    assert robust_z(contaminated) == pytest.approx(robust_z(clean), rel=1e-12)

    # The plain detector is NOT immune: the outlier inflates std so much it now MISSES the
    # genuine move (fires clean, collapses on contamination) — the failure robust_z avoids.
    assert _plain_z_of_latest(clean) > 3.0
    assert _plain_z_of_latest(contaminated) < 3.0


def test_robust_z_flat_reference_is_quiet_not_infinite() -> None:
    # A flat reference has MAD = 0; a naive z would divide by zero. robust_z returns 0.0
    # (defer to Page-Hinkley) instead of +inf on the tiniest wiggle.
    assert robust_z([5.0, 5.0, 5.0, 5.0, 5.0, 7.0]) == 0.0


def test_robust_z_too_short_series_is_zero() -> None:
    assert robust_z([5.0]) == 0.0
    assert robust_z([]) == 0.0


# --------------------------------------------------------------------------------------------
# PageHinkley  (change-point detector)
# --------------------------------------------------------------------------------------------

# Committed zero-mean noise: with delta=0.5 the per-step magnitude tolerance swamps the noise,
# so neither cumulative sum ever climbs to lambda. NO randomness — a fixed, hand-authored array.
_WHITE_NOISE = [
    0.2, -0.1, 0.15, -0.2, 0.1, -0.15, 0.05, -0.05, 0.2, -0.1,
    0.1, -0.2, 0.15, -0.1, 0.05, -0.15, 0.1, -0.05, 0.2, -0.2,
]
# Genuine upward level shift (0 -> 5) and downward shift (10 -> 0), 8 flat ticks each side.
_SHIFT_UP = [0.0] * 8 + [5.0] * 8
_SHIFT_DOWN = [10.0] * 8 + [0.0] * 8


def test_page_hinkley_quiet_on_white_noise() -> None:
    ph = PageHinkley(delta=0.5, lambda_=5.0)
    fired = [ph.update(x) for x in _WHITE_NOISE]
    assert not any(fired)  # a stable market never trips the change-point alarm


def test_page_hinkley_fires_on_upward_shift() -> None:
    ph = PageHinkley(delta=0.5, lambda_=5.0)
    fired = [ph.update(x) for x in _SHIFT_UP]
    assert not any(fired[:8])  # quiet during the flat prefix
    assert any(fired[8:])  # a genuine level shift raises the alarm


def test_page_hinkley_fires_on_downward_shift() -> None:
    # Symmetric: the detector catches drops as well as rises.
    ph = PageHinkley(delta=0.5, lambda_=5.0)
    fired = [ph.update(x) for x in _SHIFT_DOWN]
    assert not any(fired[:8])
    assert any(fired[8:])


def test_page_hinkley_is_deterministic() -> None:
    # Same ticks, two instances → identical alarm sequence (no hidden state / randomness).
    a = PageHinkley(delta=0.5, lambda_=5.0)
    b = PageHinkley(delta=0.5, lambda_=5.0)
    assert [a.update(x) for x in _SHIFT_UP] == [b.update(x) for x in _SHIFT_UP]
