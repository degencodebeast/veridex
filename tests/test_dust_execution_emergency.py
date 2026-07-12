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

from veridex.dust_execution.contracts import CancelAllAck, CancelAllTriggeredEvent
from veridex.dust_execution.emergency import DustSafetySession, SafetyController

_FROZEN_CLOCK_MS = 1_700_000_000_500


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
