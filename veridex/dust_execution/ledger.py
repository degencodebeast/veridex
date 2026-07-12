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

from veridex.dust_execution.risk import RealizedFillRecord, RiskAccumulator
from veridex.store import Store


async def append_fill(store: Store, session_id: str, fill: RealizedFillRecord) -> int:
    """Persist one REAL venue-reconciled fill to the durable append-only ledger.

    Args:
        store: The durable store (append-only ``realized_fill_ledger``).
        session_id: Session identity the fill belongs to.
        fill: The real :class:`~veridex.dust_execution.risk.RealizedFillRecord` to persist.
            ``realized_pnl`` AND ``fee`` are both persisted so loss stays fee-inclusive.

    Returns:
        The store-assigned monotonic unique ``seq`` for the appended row.
    """
    return await store.append_realized_fill(
        session_id=session_id,
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

    Replays every persisted fill for ``session_id`` into a fresh accumulator in fill-time order,
    so session loss = the accumulated fee-inclusive loss over ALL fills (it persists — never
    reset to 0). It then folds a single zero-value marker fill at wall-clock ``now`` to align the
    accumulator's current-UTC-day window with ``now_fn``: this rolls the day forward past the last
    persisted fill WITHOUT changing any loss magnitude (its ``net`` is 0), so ``realized_loss_day``
    reflects ONLY the fills that land in ``now_fn``'s UTC day (0 when there are none today).

    Runs BEFORE arming so the Mode B loss caps survive a process restart.

    Args:
        store: The durable store to read the append-only ledger from.
        session_id: The session to reconstruct.
        now_fn: Returns the current time; naive values are treated as UTC. Defines "today".

    Returns:
        A fresh :class:`RiskAccumulator` seeded from the persisted ledger.
    """
    rows = await store.list_realized_fills(session_id)
    acc = RiskAccumulator(session_id=session_id)

    # Replay in fill-time order so the accumulator's forward-only UTC-day rollover is correct
    # even if rows were persisted out of chronological order (seq is the tiebreak).
    for row in sorted(rows, key=lambda r: (r.fill_ts_ms, r.seq)):
        acc.apply_realized_fill(
            RealizedFillRecord(
                realized_pnl=row.realized_pnl,
                fee=row.fee,
                session_id=session_id,
                fill_ts_ms=row.fill_ts_ms,
                source=row.source,
            )
        )

    # Align the daily window with wall-clock "now" (rolls the day forward past the last fill).
    now = now_fn()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now_ms = int(now.timestamp() * 1000)
    acc.apply_realized_fill(
        RealizedFillRecord(
            realized_pnl=0.0,
            fee=0.0,
            session_id=session_id,
            fill_ts_ms=now_ms,
        )
    )
    return acc
