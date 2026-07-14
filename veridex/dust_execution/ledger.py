"""E2-T2 — durable, append-only realized-fill/PnL ledger + restart risk reconstruction (SAF-002c).

This module is the adapter between the durable, primitive-column ledger owned by
:mod:`veridex.store` (``append_realized_fill`` / ``list_realized_fills`` — the append-only,
unique-seq, fee-inclusive table that MIRRORS the ``competition_events`` pattern, NOT the
``execution_records`` upsert) and the in-memory :class:`~veridex.dust_execution.risk.RiskAccumulator`.

Its reason for existing (SAF-002b/c, AC-033): the realized-loss caps that fail-close Mode B must
survive a process restart. ``reconstruct_risk`` runs BEFORE arming — it replays the persisted
ledger into a FRESH accumulator so:

  * **session loss** accumulates over ALL persisted fills for the session (persists across a
    restart — NEVER reset to 0); and
  * **daily loss** counts ONLY the fills whose ``fill_ts_ms`` falls in the CURRENT UTC day per
    ``now_fn`` (rolls over at UTC midnight — a prior-UTC-day fill does NOT count toward today's
    daily loss but DOES count toward session loss).

Fee-inclusiveness is preserved end to end: both ``realized_pnl`` and ``fee`` are persisted and
the accumulator folds ``net = realized_pnl - fee`` per fill.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from veridex.dust_execution.risk import FailClosed, RealizedFillRecord, RiskAccumulator
from veridex.store import Store


def _utc_day(ts_ms: int) -> datetime:
    """Return the UTC-midnight day boundary for an integer epoch-millisecond timestamp."""
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _now_utc() -> datetime:
    """Default append-time clock (real wall-clock, UTC)."""
    return datetime.now(UTC)


async def append_fill(
    store: Store,
    fill: RealizedFillRecord,
    *,
    now_fn: Callable[[], datetime] = _now_utc,
) -> int:
    """Persist one REAL venue-reconciled fill to the durable append-only ledger.

    The fill is ALWAYS persisted under its OWN immutable ``fill.session_id`` — there is no separate
    session argument a caller could disagree with. This closes the durable-append rebind gap
    (Gate#1 MAJOR-2): the redundant ``session_id`` parameter used to be persisted verbatim, so
    ``append_fill(store, "B", fill_for_A)`` bound A's real loss under B and A UNDER-counted it on
    reconstruct (unsafe direction). Making the record's identity authoritative makes that mismatch
    unrepresentable.

    Fails closed on a FUTURE-DATED fill (``fill_ts_ms`` after append-time ``now``): a realized fill
    is by definition a past event, so a timestamp ahead of ``now`` is corrupt (or venue clock skew).
    Rejecting it at the append boundary keeps such a row from ever entering the durable ledger and
    poisoning the UTC-day loss reconstruction (SAF-002c / Gate#1 MINOR-1). The rejection RAISES
    (fail-closed) rather than silently dropping, so the caller must treat it as a stop condition.

    Args:
        store: The durable store (append-only ``realized_fill_ledger``).
        fill: The real :class:`~veridex.dust_execution.risk.RealizedFillRecord` to persist. Its
            ``session_id`` is authoritative; ``realized_pnl`` AND ``fee`` are both persisted so
            loss stays fee-inclusive.
        now_fn: Append-time clock; naive values are treated as UTC. Defaults to real wall-clock UTC.

    Returns:
        The store-assigned monotonic unique ``seq`` for the appended row.

    Raises:
        FailClosed: ``fill.fill_ts_ms`` is dated after append-time ``now`` (future/corrupt).
    """
    now = now_fn()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now_ms = int(now.timestamp() * 1000)
    if fill.fill_ts_ms > now_ms:
        raise FailClosed(
            f"refusing to append a future-dated realized fill: fill_ts_ms={fill.fill_ts_ms} is "
            f"after append-time now={now_ms} (a realized fill is a past event; a future/corrupt "
            "timestamp would poison the durable ledger and the UTC-day loss reconstruction)"
        )
    return await store.append_realized_fill(
        session_id=fill.session_id,
        realized_pnl=fill.realized_pnl,
        fee=fill.fee,
        fill_ts_ms=fill.fill_ts_ms,
        source=fill.source,
    )


async def reconstruct_risk(
    store: Store,
    session_id: str,
    now_fn: Callable[[], datetime],
) -> RiskAccumulator:
    """Rebuild a FRESH :class:`RiskAccumulator` from the durable ledger (restart-safe).

    Computes the seed totals by a DIRECT UTC-day filter over the persisted ledger:

      * session loss = fee-inclusive loss over ALL persisted fills (persists — never reset to 0);
      * daily loss   = fee-inclusive loss over ONLY the fills whose ``fill_ts_ms`` lands in
        ``now_fn``'s UTC day (0 when there are none today).

    Each row is rebuilt into a :class:`RealizedFillRecord` so the finiteness validation still runs
    on replay, and its fee-inclusive ``net`` is summed into the session total and — iff its UTC day
    equals ``now``'s UTC day — the daily total. The result is seeded into a fresh accumulator whose
    ``current_day`` is ``now``'s UTC day, so subsequent LIVE fills roll the daily window correctly.

    This is correct regardless of fill ORDER or venue clock skew: a fill dated on a LATER UTC day
    than ``now`` (skew across UTC midnight, or a corrupt/future timestamp) counts toward the session
    total but is EXCLUDED from the daily total — it can no longer advance the daily window past
    today and drop today's real losses (the forward-only-rollover under-count of the prior
    marker-replay path — SAF-002c / Gate#1 MINOR-1).

    Runs BEFORE arming so the Mode B loss caps survive a process restart.

    Args:
        store: The durable store to read the append-only ledger from.
        session_id: The session to reconstruct.
        now_fn: Returns the current time; naive values are treated as UTC. Defines "today".

    Returns:
        A fresh :class:`RiskAccumulator` seeded from the persisted ledger.
    """
    rows = await store.list_realized_fills(session_id)

    now = now_fn()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    today = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    net_session = 0.0
    net_day = 0.0
    for row in rows:
        # Rebuild the real record from the row's OWN persisted identity + provenance (never the
        # query arg) so replay preserves the fill's true session and a corrupt-source row fails
        # closed here (Gate#1 MAJOR-1/2); the finiteness validation also still runs on replay.
        record = RealizedFillRecord(
            realized_pnl=row.realized_pnl,
            fee=row.fee,
            session_id=row.session_id,
            fill_ts_ms=row.fill_ts_ms,
            source=row.source,
        )
        net = record.net_pnl()
        net_session += net
        if _utc_day(row.fill_ts_ms) == today:
            net_day += net

    return RiskAccumulator.seeded(
        session_id=session_id,
        net_session=net_session,
        net_day=net_day,
        current_day=today,
    )
