"""Falsification statistic for market-maker quote quality.

This is the statistic that gives the maker scorer its teeth. It compares a
naive negative-control maker against a TxLINE-fair candidate on a **shared
tape** of forward markouts and reports whether the two populations *separate*
in quote-quality terms, with a deterministic bootstrap confidence interval.

The comparison is framed strictly as **quote quality** (never "edge", "profit"
or "pnl"): a higher mean markout is a better quote, and a candidate is only
declared to have separated from the negative control when the entire bootstrap
CI of the mean-markout delta sits above zero.

Determinism is a hard requirement: a fixed ``seed`` yields a sealed,
reproducible verdict. The bootstrap uses only the standard library
(:mod:`random`, :mod:`statistics`) so the resampling draw is fully pinned by
the seed with no third-party RNG in the path.
"""

from __future__ import annotations

import random
from statistics import mean

from pydantic import BaseModel

__all__ = ["FalsificationResult", "falsify"]


class FalsificationResult(BaseModel):
    """Sealed verdict of a naive-vs-candidate quote-quality falsification.

    Attributes:
        delta_bps: Candidate-minus-naive mean markout, in basis points.
        ci_low_bps: 2.5th-percentile bootstrap bound of the delta, in bps.
        ci_high_bps: 97.5th-percentile bootstrap bound of the delta, in bps.
        verdict: One of ``"SEPARATED"``, ``"INVERTED"`` or ``"INCONCLUSIVE"``.
    """

    delta_bps: int
    ci_low_bps: int
    ci_high_bps: int
    verdict: str


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Return the ``pct`` percentile of an already-sorted list (nearest-rank).

    Args:
        sorted_values: Non-empty list sorted in ascending order.
        pct: Percentile in ``[0.0, 1.0]`` (e.g. ``0.025`` for the 2.5th).

    Returns:
        The value at the nearest-rank index for ``pct``.
    """
    idx = int(pct * (len(sorted_values) - 1))
    return sorted_values[idx]


def falsify(
    naive_markouts: list[int],
    candidate_markouts: list[int],
    *,
    n_boot: int = 1000,
    seed: int = 20260707,
) -> FalsificationResult:
    """Falsify that a candidate maker separates from a naive negative control.

    Computes the mean-markout delta ``mean(candidate) - mean(naive)`` and a
    deterministic bootstrap confidence interval for that delta, then renders a
    sealed verdict:

    * ``"SEPARATED"`` when the whole CI is above zero (``ci_low > 0``): the
      candidate's quote quality reliably beats the negative control.
    * ``"INVERTED"`` when the whole CI is below zero (``ci_high < 0``): the
      candidate is reliably *worse* than the control.
    * ``"INCONCLUSIVE"`` when the CI straddles zero.

    The bootstrap draws with ``random.Random(seed)`` and resamples **both**
    tapes with replacement (each at its own length) on every iteration, so the
    verdict is fully reproducible for a fixed seed.

    Args:
        naive_markouts: Forward markouts (bps) of the naive negative control.
        candidate_markouts: Forward markouts (bps) of the TxLINE-fair candidate.
        n_boot: Number of bootstrap resamples. Defaults to ``1000``.
        seed: RNG seed pinning the resampling draw. Defaults to ``20260707``.

    Returns:
        A :class:`FalsificationResult` with the delta, CI bounds and verdict.
    """
    delta = mean(candidate_markouts) - mean(naive_markouts)

    rng = random.Random(seed)
    boot_deltas: list[float] = []
    n_naive = len(naive_markouts)
    n_cand = len(candidate_markouts)
    for _ in range(n_boot):
        res_naive = rng.choices(naive_markouts, k=n_naive)
        res_cand = rng.choices(candidate_markouts, k=n_cand)
        boot_deltas.append(mean(res_cand) - mean(res_naive))

    boot_deltas.sort()
    ci_low = _percentile(boot_deltas, 0.025)
    ci_high = _percentile(boot_deltas, 0.975)

    if ci_low > 0:
        verdict = "SEPARATED"
    elif ci_high < 0:
        verdict = "INVERTED"
    else:
        verdict = "INCONCLUSIVE"

    return FalsificationResult(
        delta_bps=round(delta),
        ci_low_bps=round(ci_low),
        ci_high_bps=round(ci_high),
        verdict=verdict,
    )
