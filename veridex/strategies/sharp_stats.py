"""Pure sharp-move statistics for momentum v2 (REQ-2D-502).

Deterministic, side-effect-free math ONLY: no I/O, no LLM SDK, no network, no randomness, no
wall-clock. Every function is a total function of its inputs, so a backtest over the same ticks
yields byte-identical results (the backtest-integrity property the momentum v2 layer depends on).

Primitives
----------
* :func:`logit` — logit-space (log-odds) probability movement; probabilities move additively in
  logit space, which linearises the "distance" of a price move regardless of its starting level.
* :func:`ewma` — exponential weighted moving average; used to normalise recent volatility.
* :func:`robust_z` — median/MAD robust z-score of the latest point vs its reference window. MAD is
  scaled by the ``1.4826`` consistency constant so that ``1.4826 * MAD`` estimates the standard
  deviation for normally-distributed data. Immune to a lone outlier in the reference that would
  corrupt a mean/std z-score (median and MAD have a 50% breakdown point).
* :class:`PageHinkley` — cumulative-sum change-point detector; confirms a *sustained* level shift
  and stays quiet on zero-mean noise, in either direction.
"""

from __future__ import annotations

import math
from statistics import median

# Symmetric clamp epsilon for the logit domain guard. TxLINE probabilities live strictly inside
# (0, 1); this only fires on a boundary/out-of-range feed value, keeping the log-odds finite (and
# symmetric: a symmetric clamp preserves ``logit(p) == -logit(1 - p)``) rather than raising.
_LOGIT_EPS = 1e-12

# MAD -> sigma consistency constant (1 / 0.674489...): 1.4826 * MAD ≈ std for normal data.
_MAD_TO_SIGMA = 1.4826


def logit(p: float) -> float:
    """Return the log-odds ``ln(p / (1 - p))``, clamped to the open interval ``(0, 1)``.

    The clamp is symmetric (``p`` into ``[eps, 1 - eps]``), so the reflection symmetry
    ``logit(p) == -logit(1 - p)`` holds even at the guarded boundary.

    Args:
        p: A probability. Values at or outside ``{0, 1}`` are clamped, not rejected.

    Returns:
        The finite log-odds of ``p``.
    """
    q = min(max(p, _LOGIT_EPS), 1.0 - _LOGIT_EPS)
    return math.log(q / (1.0 - q))


def ewma(values: list[float], alpha: float) -> float:
    """Exponential weighted moving average of ``values`` (oldest first).

    Recurrence ``s_0 = x_0``; ``s_t = alpha * x_t + (1 - alpha) * s_{t-1}``. ``alpha`` near 1
    reacts fast (``alpha == 1`` returns the last value); ``alpha`` near 0 reacts slowly.

    Args:
        values: Non-empty series, oldest first.
        alpha: Smoothing factor in ``(0, 1]``.

    Returns:
        The EWMA of the series (its final smoothed value).

    Raises:
        ValueError: If ``values`` is empty or ``alpha`` is outside ``(0, 1]``.
    """
    if not values:
        raise ValueError("ewma requires at least one value")
    if not (0.0 < alpha <= 1.0):
        raise ValueError(f"alpha must be in (0, 1], got {alpha!r}")
    smoothed = values[0]
    for value in values[1:]:
        smoothed = alpha * value + (1.0 - alpha) * smoothed
    return smoothed


def robust_z(series: list[float]) -> float:
    """Robust z-score of the LATEST point vs its reference window (median/MAD).

    The reference is ``series[:-1]`` (the latest point's own history), so the scored point never
    contaminates its own location/scale estimate. With ``med = median(reference)`` and
    ``MAD = median(|x - med|)``, the score is ``(series[-1] - med) / (1.4826 * MAD)``.

    Returns ``0.0`` (quiet — defer to the change-point detector) when there is no dispersion to
    judge against: fewer than two points, or a flat reference where ``MAD == 0`` (which would
    otherwise divide by zero).

    Args:
        series: Recent observations, oldest first; the last element is the point being scored.

    Returns:
        The robust z-score of the latest point, or ``0.0`` when it cannot be estimated.
    """
    if len(series) < 2:
        return 0.0
    reference = series[:-1]
    med = median(reference)
    mad = median([abs(x - med) for x in reference])
    scale = _MAD_TO_SIGMA * mad
    if scale == 0.0:
        return 0.0
    return (series[-1] - med) / scale


class PageHinkley:
    """Page-Hinkley change-point detector (deterministic, stateful per instance).

    Tracks a running mean and two cumulative sums — one that grows on a sustained *increase* and
    one that grows on a sustained *decrease*. :meth:`update` returns ``True`` the moment either
    accumulated deviation exceeds ``lambda_``. ``delta`` is the per-step magnitude tolerance that
    absorbs zero-mean noise (so noise never trips the alarm); ``lambda_`` is the detection
    threshold (larger → later, more conservative alarms).
    """

    def __init__(self, *, delta: float, lambda_: float) -> None:
        """Initialise the detector.

        Args:
            delta: Magnitude tolerance per step (absorbs noise; typically small and positive).
            lambda_: Alarm threshold on the accumulated deviation.
        """
        self._delta = delta
        self._lambda = lambda_
        self._n = 0
        self._mean = 0.0
        self._m_hi = 0.0  # cumulative (x - mean - delta): climbs on a sustained increase
        self._min_hi = 0.0
        self._m_lo = 0.0  # cumulative (x - mean + delta): sinks on a sustained decrease
        self._max_lo = 0.0

    def update(self, x: float) -> bool:
        """Feed the next observation; return whether a change-point is confirmed at this step.

        Args:
            x: The next scalar observation.

        Returns:
            ``True`` iff the accumulated increase- or decrease-deviation now exceeds ``lambda_``.
        """
        self._n += 1
        self._mean += (x - self._mean) / self._n
        self._m_hi += x - self._mean - self._delta
        self._min_hi = min(self._min_hi, self._m_hi)
        self._m_lo += x - self._mean + self._delta
        self._max_lo = max(self._max_lo, self._m_lo)
        ph_hi = self._m_hi - self._min_hi  # >= 0, grows on a sustained upward shift
        ph_lo = self._max_lo - self._m_lo  # >= 0, grows on a sustained downward shift
        return ph_hi > self._lambda or ph_lo > self._lambda
