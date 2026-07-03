"""Edge-legibility helpers (REQ-2D-501) — EXPLANATORY, never law, never a score.

These pure helpers turn a REAL venue quote + the TxLINE de-margined fair probability into the
two EXPLANATORY quantities the flagship surfaces show ALONGSIDE the law's
``executable_edge_bps``:

  * ``venue_implied_prob_bps`` — the venue's decimal price re-expressed as an implied probability
    in basis points: ``round(10000 / venue_decimal_price)``. What the book is naively pricing.
  * ``mispricing_gap_bps`` — ``fair_prob_bps - venue_implied_prob_bps``. A PROBABILITY-SPACE
    dislocation between TxLINE's de-margined fair value and the venue's implied probability.
    Explanatory ONLY — NEVER labeled "edge," NEVER scored (spec §2 / CON-2D-501).

Distinct from :func:`veridex.law.edge.executable_edge_bps` (the LAW-owned EV form
``round((fair_prob * venue_decimal_price - 1) * 10000)`` that GATES execution): the gap lives in
probability space, the edge in expected-value space. The law is UNCHANGED — this module never
imports, re-derives, or re-de-vigs it. At the fair decimal price (``1/p``) BOTH the gap and the
edge are exactly ``0``, because ``prob_bps`` is already the de-margined consensus (never re-vigged).

Trust path: pure, sync, LLM-free.
"""

from __future__ import annotations


def venue_implied_prob_bps(venue_decimal_price: float) -> int:
    """Venue decimal price → implied probability (bps): ``round(10000 / venue_decimal_price)``.

    Args:
        venue_decimal_price: The decimal odds actually quoted at the venue.

    Returns:
        The book-implied probability in basis points. ``0`` when ``venue_decimal_price <= 0``
        (no real quote ⇒ no implied probability — fail-safe, never raises; mirrors the law's
        non-positive-price guard).
    """
    if venue_decimal_price <= 0.0:
        return 0
    return round(10000 / venue_decimal_price)


def mispricing_gap_bps(fair_prob_bps: int, venue_decimal_price: float) -> int:
    """Probability-space dislocation ``fair_prob_bps - venue_implied_prob_bps`` (bps).

    EXPLANATORY only — NEVER an edge, NEVER scored (spec §2 / CON-2D-501). A prob-space gap is
    NOT expected value: use :func:`veridex.law.edge.executable_edge_bps` for the EV that gates
    execution.

    Args:
        fair_prob_bps: TxLINE de-margined consensus fair probability for the side, in bps.
        venue_decimal_price: The decimal odds actually quoted at the venue.

    Returns:
        ``fair_prob_bps - venue_implied_prob_bps(venue_decimal_price)`` in basis points (may be
        negative). When the price is non-positive the implied probability is ``0`` by definition,
        so callers MUST gate on a REAL quote before presenting the result.
    """
    return fair_prob_bps - venue_implied_prob_bps(venue_decimal_price)
