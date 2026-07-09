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

import bisect
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
        fv_history: FV points to scan. No ordering or dedup is assumed — eligibility is a full
            linear scan, so RAW arrival history (corrections included) is the correct input for
            replay. Do NOT pre-dedupe by ``source_ts`` (that would erase pre-correction values
            needed by :func:`replay_align`).
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


def assert_append_order(history: list[FvPoint]) -> None:
    """Assert ``sequence_no`` is a true append order: greater ``sequence_no`` never arrives earlier.

    ``sequence_no`` is the monotonic arrival order and is the freshness tie-break on equal
    ``source_ts`` (:func:`eligible_fv`). For that tie-break to mean "latest arrival wins", a later
    ``sequence_no`` MUST carry a non-decreasing ``recv_ts``. This validates that invariant so a
    malformed history cannot silently corrupt no-look-ahead alignment.

    Args:
        history: FV points to validate; checked in ``sequence_no`` order.

    Raises:
        ValueError: if a point with a greater ``sequence_no`` has a strictly earlier ``recv_ts``.
    """
    prev: FvPoint | None = None
    for point in sorted(history, key=lambda p: p.sequence_no):
        if prev is not None and point.recv_ts < prev.recv_ts:
            raise ValueError(
                f"append-order violation: sequence_no {point.sequence_no} has recv_ts "
                f"{point.recv_ts} < sequence_no {prev.sequence_no} recv_ts {prev.recv_ts}"
            )
        prev = point


def _insort_fv(history: list[FvPoint], point: FvPoint) -> None:
    """Insert ``point`` keeping ``history`` ascending by ``source_ts`` and DEDUPED (latest value wins).

    Mirrors ``scripts/maker/live_monitor.py::_insort_fv``: on a duplicate ``source_ts`` the latest
    arrival replaces the prior point (a corrected/refreshed snapshot supersedes). This is the LIVE
    forward-alignment shape — it deliberately DISCARDS superseded arrivals, so it must NOT be used to
    pre-collapse history before :func:`replay_align` (that would erase the very pre-correction values
    replay needs — see :func:`replay_align`).
    """
    keys = [p.source_ts for p in history]
    pos = bisect.bisect_left(keys, point.source_ts)
    if pos < len(history) and history[pos].source_ts == point.source_ts:
        history[pos] = point  # duplicate source_ts → keep the latest arrival
    else:
        history.insert(pos, point)


def replay_align(
    decisions: list[tuple[str, int]], fv_history: list[FvPoint]
) -> dict[str, FvPoint | None]:
    """Reconstruct each decision's aligned FV from the state eligible AT THAT decision's ``recv_ts``.

    Correctness contract (the whole point of E2): each decision is aligned against the RAW arrival
    history under per-decision eligibility — NOT against a final deduped ``source_ts → latest value``
    map. Collapsing duplicates to their latest value before eligibility would let a LATER correction
    (which had not yet arrived) rewrite an EARLIER decision, silently re-manufacturing a latency edge.

    Because eligibility (``recv_ts <= decision_recv_ts``) is applied per-decision over the full
    history, a superseding correction that arrived after a decision is simply ineligible for it, and
    the decision correctly sees the value that had arrived by its own ``recv_ts``.

    Args:
        decisions: ``(decision_id, decision_recv_ts)`` pairs; ``recv_ts`` is integer **milliseconds**.
        fv_history: The RAW FV arrival history (all arrivals, corrections included — NOT pre-deduped).

    Returns:
        A ``decision_id → aligned FvPoint`` mapping; the value is ``None`` when the decision had no
        eligible FV (abstain — never imputed).
    """
    return {
        decision_id: eligible_fv(fv_history, decision_recv_ts)
        for decision_id, decision_recv_ts in decisions
    }
