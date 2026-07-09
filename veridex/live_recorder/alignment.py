"""Two-dimensional no-look-ahead alignment for the live recorder (MM-R3, milestone E2).

The maker's ``_aligned_mid`` (``veridex/maker/tape.py``) gates FV freshness on the *source*
timestamp only. That is insufficient for honest replay: a point can carry a fresh ``source_ts``
yet only have *arrived* (``recv_ts``) after a decision was made. Reading it back into that decision
would fabricate a latency edge that did not exist live.

This module adds the missing dimension. Alignment is two-dimensional and ORDER MATTERS:

1. **ELIGIBILITY (recv_ts):** keep only points that had actually ARRIVED by the decision's
   ``recv_ts`` (``point.recv_ts <= decision_recv_ts``). ``recv_ts`` is integer **milliseconds**.
2. **FRESHNESS (source_ts):** among the eligible points, take the greatest ``source_ts`` (integer
   **seconds**); tie-break on the greatest ``sequence_no``.

If nothing is eligible, abstain (return ``None``) — the value is NEVER imputed. The eligibility
filter MUST precede the freshness pick; a final "latest known value" map is FORBIDDEN as the basis
for replay because it would let a later correction rewrite an earlier decision.
"""
from __future__ import annotations

from typing import NamedTuple


class FvPoint(NamedTuple):
    """A single fair-value observation. Field order is load-bearing (positional construction is used).

    Args:
        source_ts: When the FV was produced at the source (integer **seconds**).
        recv_ts: When the FV ARRIVED at the recorder (integer **milliseconds**) — the eligibility key.
        value: The fair-value probability.
        sequence_no: Monotonic arrival sequence; the freshness tie-break on equal ``source_ts``.
    """

    source_ts: int
    recv_ts: int
    value: float
    sequence_no: int


def eligible_fv(fv_history: list[FvPoint], decision_recv_ts: int) -> FvPoint | None:
    """The aligned FV for a decision under two-dimensional no-look-ahead rules, or ``None`` to abstain.

    Args:
        fv_history: FV points kept ascending by ``source_ts`` and deduped (latest value wins on a
            duplicate ``source_ts``). See :func:`_insort_fv`.
        decision_recv_ts: The decision's arrival time (integer **milliseconds**).

    Returns:
        The eligible point with the greatest ``source_ts`` (tie-break: greatest ``sequence_no``), or
        ``None`` when no point had arrived by ``decision_recv_ts``. Never imputes.
    """
    best: FvPoint | None = None
    for point in fv_history:
        # ELIGIBILITY FIRST: only points that had ARRIVED by decision time (recv_ts in ms).
        if point.recv_ts > decision_recv_ts:
            continue
        # FRESHNESS SECOND: greatest source_ts, tie-break greatest sequence_no.
        if best is None or (point.source_ts, point.sequence_no) > (best.source_ts, best.sequence_no):
            best = point
    return best
