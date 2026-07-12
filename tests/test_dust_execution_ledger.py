"""E2-T2 — durable UTC-day realized-fill ledger + restart risk reconstruction (SAF-002c).

Covers SAF-002b/c (AC-033, §6 group 2): a real, venue-reconciled realized-fill/PnL ledger that
is APPEND-ONLY + unique-seq + fee-inclusive (mirrors the ``competition_events`` append-only
pattern, NOT the upsert ``execution_records`` pattern), plus
``reconstruct_risk(store, session_id, now_fn)`` which rebuilds a FRESH
:class:`~veridex.dust_execution.risk.RiskAccumulator` from the durable ledger BEFORE arming so
risk state survives a process restart:

  * session loss = accumulated over ALL persisted fills for the session (persists across restart,
    NEVER reset to 0);
  * daily loss  = only the fills whose ``fill_ts_ms`` falls in the CURRENT UTC day per ``now_fn``
    (rolls over at UTC midnight — a prior-UTC-day fill does NOT count toward today's daily loss
    but DOES count toward session loss).

The InMemory store path is always exercised (offline suite: no network / DB).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from veridex.dust_execution.ledger import append_fill, reconstruct_risk
from veridex.dust_execution.risk import RealizedFillRecord
from veridex.store import InMemoryStore

_SESSION = "sess-ledger-001"

# Two real fills on UTC day-1 (2026-07-06) and one on UTC day-2 (2026-07-07).
_TS_DAY1_A = int(datetime(2026, 7, 6, 9, 0, 0, tzinfo=UTC).timestamp() * 1000)
_TS_DAY1_B = int(datetime(2026, 7, 6, 18, 0, 0, tzinfo=UTC).timestamp() * 1000)
_TS_DAY2 = int(datetime(2026, 7, 7, 9, 0, 0, tzinfo=UTC).timestamp() * 1000)

# Wall-clock "now" for reconstruction is on UTC day-2 (rolls the daily window to day-2).
_NOW_DAY2 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def _fill(realized_pnl: float, fee: float, ts_ms: int) -> RealizedFillRecord:
    return RealizedFillRecord(
        realized_pnl=realized_pnl,
        fee=fee,
        session_id=_SESSION,
        fill_ts_ms=ts_ms,
    )


async def test_restart_reconstructs_session_loss_and_rolls_utc_day() -> None:
    """Persist 3 real fills spanning a UTC-day boundary, then rebuild risk from the ledger.

    day-1 net losses: (-0.30-0.01) + (-0.20-0.00) = -0.51  -> loss 0.51
    day-2 net loss:   (-0.40-0.02)                = -0.42  -> loss 0.42
    session loss = 0.51 + 0.42 = 0.93 (ALL fills, persists — never reset to 0)
    daily loss (now on day-2) = 0.42 (ONLY the current-UTC-day fills — rolled over)
    """
    store = InMemoryStore()

    seq_a = await append_fill(store, _SESSION, _fill(-0.30, 0.01, _TS_DAY1_A))
    seq_b = await append_fill(store, _SESSION, _fill(-0.20, 0.00, _TS_DAY1_B))
    seq_c = await append_fill(store, _SESSION, _fill(-0.40, 0.02, _TS_DAY2))

    # Append-only + unique monotonic sequence.
    assert seq_a < seq_b < seq_c

    # Reconstruct a FRESH accumulator from the durable ledger (as if arming after a cold start).
    acc = await reconstruct_risk(store, _SESSION, now_fn=lambda: _NOW_DAY2)
    assert acc.realized_loss_session == pytest.approx(0.93)  # ALL fills — persists
    assert acc.realized_loss_day == pytest.approx(0.42)  # only day-2 — rolled over

    # A restart mid-session reconstructs IDENTICALLY (deterministic replay of the same ledger).
    acc_restart = await reconstruct_risk(store, _SESSION, now_fn=lambda: _NOW_DAY2)
    assert acc_restart.realized_loss_session == pytest.approx(0.93)
    assert acc_restart.realized_loss_day == pytest.approx(0.42)

    # The ledger is durable across the "restart": append one more day-2 fill and rebuild again.
    await append_fill(store, _SESSION, _fill(-0.10, 0.00, _TS_DAY2))
    acc_after = await reconstruct_risk(store, _SESSION, now_fn=lambda: _NOW_DAY2)
    assert acc_after.realized_loss_session == pytest.approx(1.03)  # 0.93 + 0.10
    assert acc_after.realized_loss_day == pytest.approx(0.52)  # 0.42 + 0.10

    # Daily rolls to ZERO once "now" advances to a later UTC day with no fills that day,
    # while session loss STILL persists (never reset to 0).
    now_day3 = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)
    acc_day3 = await reconstruct_risk(store, _SESSION, now_fn=lambda: now_day3)
    assert acc_day3.realized_loss_session == pytest.approx(1.03)  # persists across the roll
    assert acc_day3.realized_loss_day == pytest.approx(0.0)  # no fills on day-3


async def test_ledger_is_append_only_and_session_scoped() -> None:
    """Fills are stored append-only with a global monotonic seq and queried per session."""
    store = InMemoryStore()
    other = "sess-ledger-OTHER"

    s1 = await append_fill(store, _SESSION, _fill(-0.10, 0.0, _TS_DAY1_A))
    s2 = await append_fill(store, other, _fill(-0.99, 0.0, _TS_DAY1_A))
    s3 = await append_fill(store, _SESSION, _fill(-0.20, 0.0, _TS_DAY1_B))

    assert s1 < s2 < s3  # monotonic across sessions (unique seq)

    rows = await store.list_realized_fills(_SESSION)
    assert [r.seq for r in rows] == [s1, s3]  # only this session, seq-ordered
    assert all(r.session_id == _SESSION for r in rows)

    # The other session's fill never leaks into this session's reconstruction.
    acc = await reconstruct_risk(store, _SESSION, now_fn=lambda: _NOW_DAY2)
    assert acc.realized_loss_session == pytest.approx(0.30)  # 0.10 + 0.20, NOT 0.99
