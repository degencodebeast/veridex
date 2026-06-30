"""Forward executable edge — the live-execution counterpart of backward-looking CLV.

GLOSSARY (the disambiguation the master plan requires):

  * ``clv_bps`` — CLOSING-LINE VALUE, backward-looking: ``closing_prob_bps[side] -
    entry_prob_bps[side]``. Only knowable once the closing line exists; a live action with no
    later horizon tick sits at the ``"pending"`` sentinel. This is the SCORED axis (the
    leaderboard ranks on it). See ``veridex.law.recompute``.
  * sealed ``edge_bps`` — in Phase 1 there is NO independent fair-value source, so the only
    evidence-derived edge IS the closing-line value: ``edge_bps == clv_bps`` (both sealed by the
    law). It is a backward measure too.
  * ``executable_edge_bps`` (THIS module) — FORWARD-looking, computed at DECISION time against
    the *actual executable venue price*: the expected value of taking the de-vigged consensus
    probability at that price. It needs no future tick, so the policy gate can use it to reject a
    take whose edge has decayed at the price on offer. It is NEVER a scoring axis (SEC-005) — it
    only gates execution.

Strategy Alpha Doctrine (master/build plan): ``prob_bps`` is the TxLINE DE-MARGINED consensus
FAIR probability — TxLINE already de-vigs the consensus, so this module NEVER re-de-vigs;
``executable_edge_bps`` is the EV at the ACTUAL venue decimal price; and capped fractional Kelly
(``veridex.execution.runner._size_stake``) is POLICY execution sizing ONLY — never a leaderboard or
proof metric (SEC-005). The leaderboard ranks on ``clv_bps`` alone.

Trust path (CON-007): pure, LLM-free.
"""

from __future__ import annotations


def executable_edge_bps(prob_bps: int, executable_price: float) -> int:
    """Forward +EV edge (bps) of taking ``prob_bps`` at ``executable_price`` (decimal odds).

    ``edge = p * price - 1`` where ``p = prob_bps / 10000`` is the de-vigged consensus
    probability and ``price`` is the decimal odds actually quoted. Returned in basis points.

    Args:
        prob_bps: De-vigged consensus probability for the side, in basis points (0..10000).
        executable_price: The decimal odds actually on offer at the venue.

    Returns:
        The forward edge in basis points (may be negative). ``0`` when ``executable_price <= 0``
        (no executable price ⇒ no advisable edge — fail-safe, never raises).
    """
    if executable_price <= 0.0:
        return 0
    p = prob_bps / 10000.0
    return round((p * executable_price - 1.0) * 10000)
