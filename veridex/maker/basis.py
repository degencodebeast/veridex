"""Basis vs lag/edge decomposition for TxLINE-vs-venue price gaps.

For a prediction-market maker, the gap between the TxLINE fair value and the
venue's native price decomposes as::

    observed_gap = basis + lag_or_edge

The **basis** is a persistent / structural offset: TxLINE and the venue price
the *same* event through *different* instruments, so a stable component of the
gap is a pricing convention, **not** alpha. Only the **residual** left after the
basis is removed may count toward a convergence ("Reach") claim.

This module therefore reports the basis **separately** (its own field) and
exposes the convergence signal only on the residual. It deliberately exposes
**no** function that turns the raw gap into "edge" (no ``edge_from_gap`` or any
raw-gap→edge helper): the raw gap alone can never be called edge, because it
still contains the structural basis.

All price operands are native probabilities in ``[0, 1]`` and are bounds-checked
via :func:`veridex.maker.markout.assert_native_prob` **before** any arithmetic,
so a decimal-odds value can never silently reach the decomposition math.
"""

from __future__ import annotations

import statistics

from pydantic import BaseModel

from veridex.maker.markout import assert_native_prob

__all__ = ["BasisDecomposition", "decompose_gap", "reach_from_residual"]


class BasisDecomposition(BaseModel):
    """Decomposition of the TxLINE-vs-venue gap into basis and residual.

    Attributes:
        basis_bps: The persistent / structural offset in basis points (the
            median gap). This is NOT alpha; it is a pricing convention.
        residual_gap_bps: The per-step residual gaps in basis points, i.e. the
            observed gap with the structural ``basis_bps`` removed. The
            convergence signal is read from this, never from the raw gap.
        n: The number of paired observations.
    """

    basis_bps: int
    residual_gap_bps: list[int]
    n: int


def decompose_gap(
    txline_fv: list[float], venue_native: list[float]
) -> BasisDecomposition:
    """Decompose the TxLINE-vs-venue gap into a structural basis and a residual.

    Every operand in both lists is bounds-checked via
    :func:`assert_native_prob` **before** any arithmetic. The per-step gaps (in
    basis points) are computed, the persistent structural offset is taken as the
    **median** gap (the ``basis_bps``), and the residual is what remains after
    removing that basis from each step.

    Args:
        txline_fv: TxLINE fair-value native probabilities in ``[0, 1]``.
        venue_native: Venue native-price probabilities in ``[0, 1]``. Assumed to
            be the same length as ``txline_fv``.

    Returns:
        A :class:`BasisDecomposition` with the structural ``basis_bps`` reported
        separately from the ``residual_gap_bps``.

    Raises:
        MarkoutError: If any operand is not a native probability in ``[0, 1]``.
    """
    if len(txline_fv) != len(venue_native):
        raise ValueError("decompose_gap requires equal-length series")
    if not txline_fv:
        raise ValueError("decompose_gap requires non-empty series")
    n = len(txline_fv)
    for x in txline_fv:
        assert_native_prob(x, "txline_fv")
    for x in venue_native:
        assert_native_prob(x, "venue_native")
    gaps = [(txline_fv[i] - venue_native[i]) * 1e4 for i in range(n)]
    basis_bps = round(statistics.median(gaps))
    residual_gap_bps = [round(gaps[i]) - basis_bps for i in range(n)]
    return BasisDecomposition(
        basis_bps=basis_bps, residual_gap_bps=residual_gap_bps, n=n
    )


def reach_from_residual(residual_gap_bps: list[int]) -> float | None:
    """Fraction of consecutive steps where the residual magnitude shrinks.

    This is the convergence "Reach" computed on the **residual only** (the gap
    after the structural basis has been removed), never on the raw gap. A step
    counts toward Reach when the absolute residual at ``i+1`` is strictly smaller
    than at ``i`` (the residual is converging toward zero).

    Args:
        residual_gap_bps: The per-step residual gaps in basis points, as produced
            by :func:`decompose_gap`.

    Returns:
        The fraction of consecutive steps whose residual magnitude shrank, or
        ``None`` if there are fewer than two observations.
    """
    length = len(residual_gap_bps)
    if length < 2:
        return None
    shrinks = sum(
        1
        for i in range(length - 1)
        if abs(residual_gap_bps[i + 1]) < abs(residual_gap_bps[i])
    )
    return shrinks / (length - 1)
