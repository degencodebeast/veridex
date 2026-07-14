"""E2-T3 — SafetyController + idempotent cancel-all primitive (SAF-003, AC-006, §6 group 1).

Drives the REAL orchestration entry point (:class:`SafetyController`) DIRECTLY. The E6 runner
does not exist yet, so E2 must NOT test "the runner path" — it exercises the controller + its
runtime session directly so Gate #1 tests a real code path, not a runner mock (Codex-M1).

The load-bearing assertion is a RECORDING-FAKE WIRE check: the venue adapter's
``cancel_all_orders`` must be ACTUALLY awaited (the fake records the call), NOT merely a
submit-block flag flipped. A state-only assertion is forbidden — the whole point is proving the
venue sweep fired (SAF-003).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from veridex.dust_execution.contracts import CancelAllAck, CancelAllTriggeredEvent
from veridex.dust_execution.emergency import (
    DustSafetySession,
    ReArmDenied,
    SafetyController,
)
from veridex.dust_execution.ledger import reconstruct_risk
from veridex.store import InMemoryStore

_FROZEN_CLOCK_MS = 1_700_000_000_500
_FROZEN_NOW = datetime(2026, 7, 12, tzinfo=UTC)


class RecordingFakeAdapter:
    """Venue cancel-all test double that RECORDS the wire call was ACTUALLY awaited.

    ``calls`` increments ONLY when the ``cancel_all_orders`` coroutine is actually run/awaited —
    a mere state-flag flip inside the controller can never move it. That is the whole point: the
    test proves the venue sweep fired, not that a boolean was set.
    """

    def __init__(self, *, canceled_count: int = 3) -> None:
        self.calls = 0
        self._canceled_count = canceled_count

    async def cancel_all_orders(self) -> int:
        self.calls += 1
        return self._canceled_count


class FakeReconciler:
    """Open-order reconciliation test double for the re-arm precondition (SAF-002c).

    ``open_order_count`` is the reconciliation seam the SEPARATE re-arm op consults: re-arm may
    only proceed once the venue reports ZERO resting orders. ``calls`` records that the
    reconciliation was ACTUALLY awaited, not assumed.
    """

    def __init__(self, *, open_orders: int) -> None:
        self.calls = 0
        self._open_orders = open_orders

    async def open_order_count(self) -> int:
        self.calls += 1
        return self._open_orders


async def test_breaker_open_fires_wire_cancel_all_and_blocks_new_submits() -> None:
    controller = SafetyController(clock_ms=lambda: _FROZEN_CLOCK_MS)
    session = DustSafetySession(session_id="sess-1")
    rec = RecordingFakeAdapter(canceled_count=3)

    # Precondition: submits are admitted BEFORE any safety trigger fires.
    assert controller.check_can_submit(session) is True

    # Drive the REAL entry point directly (no runner — Codex-M1).
    ack = await controller.on_breaker_open(adapter=rec, session=session)

    # WIRE assertion (load-bearing recording-fake rule): the venue cancel-all was ACTUALLY
    # awaited exactly once — NOT just a block flag flipped.
    assert rec.calls == 1

    # A CancelAllAck is emitted carrying only the CAUSE + swept count, never a single order id.
    assert isinstance(ack, CancelAllAck)
    assert ack.trigger_cause == "breaker"
    assert ack.canceled_count == 3
    assert "venue_order_id" not in ack.model_dump()

    # The CancelAllTriggeredEvent was emitted into the session stream, cause-only (SAF-003).
    triggered = [e for e in session.events if isinstance(e, CancelAllTriggeredEvent)]
    assert len(triggered) == 1
    assert triggered[0].trigger_cause == "breaker"
    assert "venue_order_id" not in triggered[0].model_dump()

    # A subsequent submit is now BLOCKED — the observable seam the E6 runner consults later.
    assert controller.check_can_submit(session) is False
    assert session.submit_blocked is True

    # Idempotent: a SECOND on_breaker_open (e.g. a retry) is a no-op that STAYS blocked and does
    # NOT re-fire the wire — it never re-opens submits.
    ack2 = await controller.on_breaker_open(adapter=rec, session=session)
    assert rec.calls == 1  # wire NOT fired again
    assert isinstance(ack2, CancelAllAck)
    assert ack2.trigger_cause == "breaker"
    assert controller.check_can_submit(session) is False
    assert session.submit_blocked is True


async def test_duplicate_engage_is_noop_and_never_re_enables() -> None:
    """Engage-only kill-switch: idempotent stop, and only a SEPARATE reconciled op re-arms (SAF-004).

    Drives ``on_kill_switch`` directly (E6 runner does not exist yet). The FIRST engage fires the
    ONE cancel-all wire once + blocks; a DUPLICATE engage (a retry after an uncertain control-plane
    ACK) is a pure NO-OP that STAYS engaged, does NOT re-fire the wire, and re-enables NOTHING. The
    stop path can NEVER re-arm — only the SEPARATE :meth:`SafetyController.re_arm`, and only with
    ALL three preconditions (open-order reconciliation + risk-state reload + operator auth), clears
    it; missing any one FAILS CLOSED (stays blocked).
    """
    controller = SafetyController(clock_ms=lambda: _FROZEN_CLOCK_MS)
    session = DustSafetySession(session_id="sess-ks")
    rec = RecordingFakeAdapter(canceled_count=2)

    # Precondition: submits admitted before the kill-switch fires.
    assert controller.check_can_submit(session) is True

    # FIRST engage: the ONE cancel-all wire fires exactly once (recording-fake proves it) + blocks.
    ack = await controller.on_kill_switch(adapter=rec, session=session)
    assert rec.calls == 1  # WIRE actually awaited — not a flag flip
    assert isinstance(ack, CancelAllAck)
    assert ack.trigger_cause == "kill_switch"
    assert ack.canceled_count == 2
    assert controller.check_can_submit(session) is False
    assert session.submit_blocked is True

    # DUPLICATE engage (client retry after an uncertain ACK): NO-OP. Stays engaged, wire NOT
    # re-fired in a way that could re-open trading, no submit possible.
    ack2 = await controller.on_kill_switch(adapter=rec, session=session)
    assert rec.calls == 1  # wire NOT fired a second time
    assert isinstance(ack2, CancelAllAck)
    assert ack2.trigger_cause == "kill_switch"
    assert controller.check_can_submit(session) is False
    assert session.submit_blocked is True

    # A genuinely reloaded risk-state (SAF-002c) — the real reconstruct_risk path, not a stub.
    reloaded_risk = await reconstruct_risk(
        InMemoryStore(), "sess-ks", now_fn=lambda: _FROZEN_NOW
    )

    # Re-arm is IMPOSSIBLE via the stop path. The SEPARATE re_arm op FAILS CLOSED when ANY of the
    # three preconditions is unmet — the stop stays engaged every time.

    # (1) Open orders still resting → reconciliation fails → stays blocked.
    dirty = FakeReconciler(open_orders=2)
    with pytest.raises(ReArmDenied):
        await controller.re_arm(
            session=session,
            reconciler=dirty,
            risk=reloaded_risk,
            operator_authorized=True,
        )
    assert session.submit_blocked is True

    # (2) No explicit operator authorization → stays blocked.
    clean = FakeReconciler(open_orders=0)
    with pytest.raises(ReArmDenied):
        await controller.re_arm(
            session=session,
            reconciler=clean,
            risk=reloaded_risk,
            operator_authorized=False,
        )
    assert session.submit_blocked is True

    # (3) No risk-state reload (SAF-002c) → stays blocked.
    with pytest.raises(ReArmDenied):
        await controller.re_arm(
            session=session,
            reconciler=FakeReconciler(open_orders=0),
            risk=None,
            operator_authorized=True,
        )
    assert session.submit_blocked is True

    # ALL three preconditions positively satisfied → the SEPARATE op re-arms (and only it can).
    await controller.re_arm(
        session=session,
        reconciler=FakeReconciler(open_orders=0),
        risk=reloaded_risk,
        operator_authorized=True,
    )
    assert session.submit_blocked is False
    assert session.block_cause is None
    assert controller.check_can_submit(session) is True
