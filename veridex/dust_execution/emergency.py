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
  breaker-open route (:meth:`on_breaker_open`) is E2-T3; the kill-switch (:meth:`on_kill_switch`,
  E2-T4) and loss-breach (:meth:`on_loss_breach` + the atomic :meth:`on_realized_fill`
  coordinating method, E2-T5) routes are thin siblings that add a one-line call to this SAME
  primitive; the shutdown (E6-T6) route lands under its own task with its own TDD RED step.
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
from veridex.dust_execution.risk import RealizedFillRecord, RiskAccumulator
from veridex.policy.envelope import PolicyEnvelope

# The two cancel-all lifecycle events the primitive emits into the session stream.
CancelAllEvent = CancelAllTriggeredEvent | CancelAllAck


class ReArmDenied(Exception):
    """A re-arm precondition could not be POSITIVELY satisfied — the emergency stop STAYS engaged.

    Raised by :meth:`SafetyController.re_arm` when open-order reconciliation is not clean, the
    risk-state was not reloaded (SAF-002c), or the operator did not explicitly authorize the
    re-arm. Fail-closed is the whole point: clearing the stop is the dangerous action, so it
    happens ONLY when every guard is green — never as a side effect of the stop path (SAF-004).
    """


@runtime_checkable
class CancelAllAdapter(Protocol):
    """Minimal venue seam the cancel-all primitive needs: sweep ALL resting orders at once.

    Mirrors the real ``DELETE /cancel-all`` venue concept (a single sweep, never a per-order
    loop) and returns the number of orders the venue reports canceled. This is the ONLY method
    the primitive touches — the controller holds no raw signer, wallet, or client handle.
    """

    async def cancel_all_orders(self) -> int: ...


@runtime_checkable
class OpenOrderReconciler(Protocol):
    """Venue seam the SEPARATE re-arm op consults: how many resting orders are STILL open.

    Re-arm may proceed only once reconciliation reports ZERO open orders — a non-zero count means
    capital is still exposed at the venue, so re-arming would re-open trading on top of dangling
    orders. Full reconciliation lands in E4; this is the minimal seam re-arm fails closed against.
    """

    async def open_order_count(self) -> int: ...


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

    async def on_kill_switch(
        self, *, adapter: CancelAllAdapter, session: DustSafetySession
    ) -> CancelAllAck:
        """Kill-switch engage trigger (E2-T4) — routes to the single primitive (``kill_switch``).

        Engage-only and idempotent by construction: the FIRST engage fires the ONE cancel-all wire
        once and blocks submits; every SUBSEQUENT engage (e.g. a client retry after an uncertain
        control-plane ACK) is a no-op that STAYS blocked, does NOT re-fire the wire, and re-enables
        nothing — the primitive never toggles. The stop can be cleared ONLY by the SEPARATE
        :meth:`re_arm` op, never by re-engaging (SAF-004).
        """
        return await self.cancel_all_and_block("kill_switch", adapter=adapter, session=session)

    async def on_loss_breach(
        self, *, adapter: CancelAllAdapter, session: DustSafetySession
    ) -> CancelAllAck:
        """Realized-loss-cap breach trigger (E2-T5) — routes to the single primitive (``loss_breach``).

        A thin sibling of :meth:`on_breaker_open` / :meth:`on_kill_switch`: it fires the ONE
        cancel-all wire under its OWN honest cause so a loss sweep is never mislabelled as a
        breaker/manual sweep (SAF-002d audit fidelity). Idempotent by construction via the
        primitive — a second breach on an already-blocked session is a no-op that STAYS blocked.
        """
        return await self.cancel_all_and_block("loss_breach", adapter=adapter, session=session)

    async def on_realized_fill(
        self,
        fill: RealizedFillRecord,
        *,
        adapter: CancelAllAdapter,
        session: DustSafetySession,
        risk: RiskAccumulator,
        envelope: PolicyEnvelope,
    ) -> CancelAllAck | None:
        """Fold a REAL fill into the accumulator; on a loss-cap crossing, ATOMICALLY block + sweep.

        This is the atomic loss-safety coordinating method (SAF-002d). Unlike ``evaluate_pre_quote``
        — which only DENIES the NEXT quote once a cap is crossed — a breach detected here
        PROACTIVELY cancels the RESTING orders: if the accumulated fee-inclusive loss crosses
        ``max_session_loss`` OR ``max_daily_loss``, it routes through :meth:`on_loss_breach` to the
        SAME idempotent :meth:`cancel_all_and_block` primitive.

        Atomicity (no fill-through window): applying the fill and checking the breach are
        synchronous (no ``await`` between them), and the sweep primitive sets the submit-block flag
        BEFORE its cancel-all ``await`` — so there is NO point at which a resting GTC order can keep
        filling after the breach is detected but before the sweep. Blocking is the fail-safe state:
        if the wire raises, submits STAY blocked.

        The paper/simulated anti-inert control is preserved: ``apply_realized_fill`` rejects a
        non-real source AT SOURCE (``ValueError``) before any breach check, so a simulated fill can
        never fire the sweep.

        Args:
            fill: A real ``RealizedFillRecord`` (a ``PaperReceipt`` is rejected at source).
            adapter: The venue cancel-all seam whose ``cancel_all_orders`` the sweep awaits.
            session: The mutable runtime session whose submit-block flag the sweep sets.
            risk: The realized-loss accumulator the fill is folded into and the caps checked against.
            envelope: The policy envelope carrying the ``max_session_loss`` / ``max_daily_loss`` caps.

        Returns:
            The ``CancelAllAck`` from the loss-breach sweep when a cap is crossed, else ``None``.
        """
        risk.apply_realized_fill(fill)
        if risk.breaches_caps(envelope):
            return await self.on_loss_breach(adapter=adapter, session=session)
        return None

    async def re_arm(
        self,
        *,
        session: DustSafetySession,
        reconciler: OpenOrderReconciler,
        risk: RiskAccumulator | None,
        operator_authorized: bool,
    ) -> None:
        """SEPARATE, fail-closed re-arm — the ONLY path that clears the emergency stop (SAF-004).

        Structurally distinct from every stop trigger: a stop can NEVER re-arm as a side effect.
        Clears ``submit_blocked`` ONLY when ALL THREE preconditions are positively satisfied, and
        raises :class:`ReArmDenied` (leaving the stop ENGAGED) the moment any one is not:

        1. **Explicit operator authorization** — a deliberate operator act, not an implicit retry.
        2. **Risk-state reload (SAF-002c)** — a fresh accumulator rebuilt from the durable ledger
           via :func:`~veridex.dust_execution.ledger.reconstruct_risk` MUST be supplied, so the
           loss caps that fail-close Mode B are re-established before trading may resume.
        3. **Open-order reconciliation** — the venue must report ZERO resting orders; any non-zero
           count means capital is still exposed, so re-arming is refused.

        The checks run auth → risk → reconciliation, evaluating the wire (reconciliation) LAST and
        only once the cheap authorization/risk guards are green. On success the block is cleared
        and the recorded block cause + ack are reset so a fresh stop starts clean.

        Args:
            session: The blocked runtime session to re-arm.
            reconciler: The open-order reconciliation seam; its ``open_order_count`` is awaited.
            risk: The reloaded risk accumulator (from ``reconstruct_risk``); ``None`` fails closed.
            operator_authorized: Whether an operator explicitly authorized this re-arm.

        Raises:
            ReArmDenied: Any precondition is not positively satisfied; the stop stays engaged.
        """
        if not operator_authorized:
            raise ReArmDenied("re-arm requires explicit operator authorization")
        if risk is None:
            raise ReArmDenied(
                "re-arm requires a reloaded risk-state (reconstruct_risk) before trading may "
                "resume (SAF-002c)"
            )
        open_orders = await reconciler.open_order_count()
        if open_orders != 0:
            raise ReArmDenied(
                f"re-arm requires zero open orders after reconciliation; {open_orders} still "
                "resting at the venue"
            )

        # All three preconditions positively satisfied — clear the stop (the ONLY place it clears).
        session.submit_blocked = False
        session.block_cause = None
        session.last_cancel_all_ack = None

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
