"""E2-T3 — SafetyController + idempotent cancel-all primitive (SAF-003, AC-006, §6 group 1).

The REAL orchestration entry point for the R4-A dust-execution emergency stop. When a safety
trigger fires — the circuit breaker opens, the realized-loss cap is breached, a kill switch is
engaged, or the session shuts down — the venue's resting orders MUST be swept AND new submits
blocked, atomically and idempotently. This is the seam the E6 runner later delegates to; the
runner does not exist yet, so E2 drives :class:`SafetyController` directly (Codex-M1).

Design (SAF-003):

* **ONE primitive.** Every trigger routes through the single idempotent
  :meth:`SafetyController.cancel_all_and_block`. There is deliberately no per-trigger cancel
  logic to drift out of sync — the venue cancel-all wire is fired in exactly ONE place, and each
  trigger only supplies its :data:`~veridex.dust_execution.contracts.CancelAllCause`. The
  breaker-open route (:meth:`on_breaker_open`) is E2-T3; the loss-breach (E2-T5), kill-switch
  (E2-T4), and shutdown (E6-T6) routes are thin siblings that add a one-line call to this SAME
  primitive under their own tasks (kept out of this diff so each keeps its own TDD RED step).
* **Recording-fake WIRE, not a flag.** The primitive AWAITS the venue adapter's
  ``cancel_all_orders`` (the real ``DELETE /cancel-all`` seam). Setting a block flag is NOT
  sufficient — the resting orders must actually be swept, so the wire call is load-bearing.
* **Block first, then sweep.** The submit-block flag is set BEFORE the ``await`` so no new submit
  can race through the suspension window while the cancel-all is in flight; and if the wire
  raises, submits STAY blocked (blocking is the fail-safe state).
* **Idempotent, stays blocked, never re-opens.** A second trigger (e.g. a breaker-open retry) is
  a no-op: it does NOT re-fire the wire and it KEEPS the block set, returning the recorded ack.
* **Cause, never an order id.** The emitted
  :class:`~veridex.dust_execution.contracts.CancelAllTriggeredEvent` /
  :class:`~veridex.dust_execution.contracts.CancelAllAck` carry only the trigger cause and (for
  the ack) the swept count — never a single order identifier (SAF-003).
* **Breaker stays pure.** ALL orchestration lives here; :mod:`veridex.policy.circuit_breaker`
  remains a pure, I/O-free state machine.

The mutable submit-block state lives on the runtime :class:`DustSafetySession` (NOT on a frozen
contract, which cannot be mutated in place). The E6 runner will own a session and consult
:meth:`SafetyController.check_can_submit` before every submit.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from veridex.dust_execution.contracts import (
    CancelAllAck,
    CancelAllCause,
    CancelAllTriggeredEvent,
)

# The two cancel-all lifecycle events the primitive emits into the session stream.
CancelAllEvent = CancelAllTriggeredEvent | CancelAllAck


@runtime_checkable
class CancelAllAdapter(Protocol):
    """Minimal venue seam the cancel-all primitive needs: sweep ALL resting orders at once.

    Mirrors the real ``DELETE /cancel-all`` venue concept (a single sweep, never a per-order
    loop) and returns the number of orders the venue reports canceled. This is the ONLY method
    the primitive touches — the controller holds no raw signer, wallet, or client handle.
    """

    async def cancel_all_orders(self) -> int: ...


def _default_clock_ms() -> int:
    """Wall-clock ``recv_ts`` in integer milliseconds (injectable for deterministic tests)."""
    return int(time.time() * 1000)


class DustSafetySession:
    """Mutable per-session runtime state the :class:`SafetyController` orchestrates against.

    Deliberately NOT a frozen contract: the submit-block flag must be flipped IN PLACE when a
    safety trigger fires, which a frozen pydantic model cannot do. Owns the monotonic event
    ``sequence_no`` source and collects the emitted cancel-all lifecycle events. One instance per
    dust-execution session.

    Attributes:
        session_id: Immutable session identity this runtime state belongs to.
        submit_blocked: Whether new submits are currently blocked (the emergency-stop flag).
        block_cause: The trigger cause that blocked submits, or ``None`` while still open.
        events: The cancel-all lifecycle events emitted for this session, in emission order.
        last_cancel_all_ack: The ack from the FIRST cancel-all, replayed on idempotent no-ops.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.submit_blocked: bool = False
        self.block_cause: CancelAllCause | None = None
        self.events: list[CancelAllEvent] = []
        self.last_cancel_all_ack: CancelAllAck | None = None
        self._next_seq: int = 0

    def next_sequence_no(self) -> int:
        """Return the next monotonic event ``sequence_no`` for this session."""
        seq = self._next_seq
        self._next_seq += 1
        return seq

    def record_event(self, event: CancelAllEvent) -> None:
        """Append an emitted cancel-all lifecycle event to the session stream."""
        self.events.append(event)


class SafetyController:
    """Stateless emergency orchestrator — the seam the E6 runner delegates to (SAF-003).

    Holds only an injected clock; ALL mutable state lives on the :class:`DustSafetySession` it
    acts on, so one controller can serve many sessions. Every safety trigger routes through the
    single :meth:`cancel_all_and_block` primitive — the one and only place the venue cancel-all
    wire is fired.
    """

    def __init__(self, *, clock_ms: Callable[[], int] | None = None) -> None:
        self._clock_ms: Callable[[], int] = clock_ms if clock_ms is not None else _default_clock_ms

    async def cancel_all_and_block(
        self,
        cause: CancelAllCause,
        *,
        adapter: CancelAllAdapter,
        session: DustSafetySession,
    ) -> CancelAllAck:
        """Sweep ALL resting orders AND block new submits — the single idempotent primitive.

        On the FIRST call for a session: sets the submit-block flag (BEFORE any ``await``, so no
        submit can race the suspension window), emits a
        :class:`~veridex.dust_execution.contracts.CancelAllTriggeredEvent`, AWAITS the venue
        ``cancel_all_orders`` wire (load-bearing — the resting orders are actually swept), then
        emits and returns a :class:`~veridex.dust_execution.contracts.CancelAllAck` carrying only
        the trigger ``cause`` and the swept count.

        A SUBSEQUENT call is a no-op: it does NOT re-fire the wire and KEEPS the block set,
        returning the recorded ack — idempotent, and it never re-opens submits.

        Args:
            cause: The trigger cause (breaker / kill_switch / shutdown / manual). Never an order id.
            adapter: The venue cancel-all seam; its ``cancel_all_orders`` is the wire fired here.
            session: The mutable runtime session whose submit-block flag is set.

        Returns:
            The :class:`CancelAllAck` for the sweep (the first ack, replayed on idempotent calls).
        """
        if session.submit_blocked:
            # Idempotent no-op: already blocked. Do NOT re-fire the wire; stay blocked.
            if session.last_cancel_all_ack is not None:
                return session.last_cancel_all_ack
            return self._build_ack(cause, canceled_count=0, session=session)

        # Block FIRST so no new submit can race through while the sweep is in flight; even if the
        # wire raises below, submits remain blocked (blocking is the fail-safe state).
        session.submit_blocked = True
        session.block_cause = cause
        session.record_event(self._build_triggered(cause, session=session))

        # THE WIRE: actually sweep the venue's resting orders — not just a flag flip.
        canceled_count = await adapter.cancel_all_orders()

        ack = self._build_ack(cause, canceled_count=canceled_count, session=session)
        session.last_cancel_all_ack = ack
        session.record_event(ack)
        return ack

    async def on_breaker_open(
        self, *, adapter: CancelAllAdapter, session: DustSafetySession
    ) -> CancelAllAck:
        """Circuit-breaker-open trigger (E2-T3) — routes to the single primitive (``breaker``)."""
        return await self.cancel_all_and_block("breaker", adapter=adapter, session=session)

    def check_can_submit(self, session: DustSafetySession) -> bool:
        """Return whether a new submit is admitted — ``False`` once cancel-all has blocked.

        The observable seam by which "a subsequent submit is blocked" is verifiable. The E6
        runner will consult this before every submit; E2 asserts it directly.
        """
        return not session.submit_blocked

    def _build_triggered(
        self, cause: CancelAllCause, *, session: DustSafetySession
    ) -> CancelAllTriggeredEvent:
        """Build the cause-only ``CancelAllTriggeredEvent`` (never carries an order id)."""
        return CancelAllTriggeredEvent(
            sequence_no=session.next_sequence_no(),
            event_type="CancelAllTriggeredEvent",
            source_ts=None,
            recv_ts=self._clock_ms(),
            trigger_cause=cause,
        )

    def _build_ack(
        self, cause: CancelAllCause, *, canceled_count: int, session: DustSafetySession
    ) -> CancelAllAck:
        """Build the ``CancelAllAck`` carrying only the cause + swept count (never an order id)."""
        return CancelAllAck(
            sequence_no=session.next_sequence_no(),
            event_type="CancelAllAck",
            source_ts=None,
            recv_ts=self._clock_ms(),
            trigger_cause=cause,
            canceled_count=canceled_count,
        )
