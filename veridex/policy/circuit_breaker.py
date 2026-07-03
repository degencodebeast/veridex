"""Pure circuit-breaker state machine for the execution guardrail (REQ-2D-404).

Deterministic and I/O-free: the SAME event sequence always yields the SAME state. Any time
input is INJECTED (no wall clock, no randomness), so the breaker is reproducible and trust-path
clean. It is the state the policy PRE-quote gate (:mod:`veridex.policy.gate`) consults; the
breaker NEVER decides on its own — the gate reads :meth:`CircuitBreaker.allows` and, on a blocked
breaker, denies with ``circuit_open``. This keeps the policy the single execution authority.

A standard circuit-breaker pattern (CLOSED/OPEN/HALF_OPEN) re-expressed in the Veridex Policy
idiom: an IMMUTABLE (frozen) pydantic model whose transitions return NEW instances, rather than
a mutating service that reads ``time.time()``.

States:
    * ``CLOSED`` — normal operation; execution allowed.
    * ``OPEN`` — too many consecutive failures; execution blocked until the cooldown elapses.
    * ``HALF_OPEN`` — cooldown elapsed; exactly ONE probe is admitted. Probe success ->
      ``CLOSED``, probe failure -> ``OPEN``.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class CircuitState(str, Enum):
    """Terminal circuit states.

    Attributes:
        CLOSED: Normal operation; execution allowed.
        OPEN: Blocking execution after repeated failures.
        HALF_OPEN: Cooldown elapsed; admitting a single recovery probe.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker(BaseModel):
    """Immutable circuit-breaker state; transitions return a new instance (pure).

    Attributes:
        state: The current circuit state.
        consecutive_failures: Execution failures observed since the last success/close.
        opened_at: Injected-clock instant the breaker last transitioned to ``OPEN`` (the
            cooldown anchor); ``None`` while never opened.
        probe_used: In ``HALF_OPEN``, whether the single admitted probe has been dispatched.
    """

    model_config = {"frozen": True}

    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float | None = None
    probe_used: bool = False

    def allows(self) -> bool:
        """Return whether execution is admitted in the current state (pure, no clock).

        ``CLOSED`` always admits; ``OPEN`` never admits; ``HALF_OPEN`` admits exactly one
        probe (until :meth:`start_probe` marks it dispatched). Time-based recovery
        (``OPEN`` -> ``HALF_OPEN``) is applied separately via :meth:`resolve`.
        """
        if self.state is CircuitState.CLOSED:
            return True
        if self.state is CircuitState.HALF_OPEN:
            return not self.probe_used
        return False  # OPEN

    def record_failure(self, *, threshold: int, now: float) -> CircuitBreaker:
        """Record an execution FAILURE and return the resulting state.

        A failure while ``HALF_OPEN`` (the probe failed) re-opens the breaker. Otherwise the
        consecutive-failure streak increments and, once it reaches ``threshold`` (``> 0``), the
        breaker opens, anchoring the cooldown at ``now``. A non-positive ``threshold`` disables
        opening entirely.

        Args:
            threshold: Consecutive failures required to open; ``<= 0`` disables the breaker.
            now: Injected clock instant to anchor the cooldown if the breaker opens.

        Returns:
            The next :class:`CircuitBreaker` state.
        """
        if self.state is CircuitState.HALF_OPEN:
            return CircuitBreaker(
                state=CircuitState.OPEN,
                consecutive_failures=self.consecutive_failures + 1,
                opened_at=now,
                probe_used=False,
            )
        failures = self.consecutive_failures + 1
        if threshold > 0 and failures >= threshold:
            return CircuitBreaker(
                state=CircuitState.OPEN,
                consecutive_failures=failures,
                opened_at=now,
                probe_used=False,
            )
        return self.model_copy(update={"consecutive_failures": failures})

    def record_success(self) -> CircuitBreaker:
        """Record an execution SUCCESS and return the resulting state.

        A success while ``HALF_OPEN`` (the probe passed) fully closes the breaker and resets
        its counters. Otherwise the consecutive-failure streak is cleared in place.
        """
        if self.state is CircuitState.HALF_OPEN:
            return CircuitBreaker()  # probe passed -> fully CLOSED, counters reset
        return self.model_copy(update={"consecutive_failures": 0})

    def resolve(self, *, now: float, cooldown_s: float) -> CircuitBreaker:
        """Apply the time-based recovery transition ``OPEN`` -> ``HALF_OPEN`` (pure).

        When the breaker is ``OPEN`` and at least ``cooldown_s`` has elapsed since it opened,
        it moves to ``HALF_OPEN`` (probe reset). All other states are returned unchanged. Time
        is INJECTED via ``now`` — the breaker never reads a wall clock.

        Args:
            now: Injected clock instant.
            cooldown_s: Seconds the breaker must stay ``OPEN`` before admitting a probe.

        Returns:
            The (possibly transitioned) :class:`CircuitBreaker` state.
        """
        if self.state is CircuitState.OPEN and self.opened_at is not None and now - self.opened_at >= cooldown_s:
            return self.model_copy(update={"state": CircuitState.HALF_OPEN, "probe_used": False})
        return self

    def start_probe(self) -> CircuitBreaker:
        """Mark the single ``HALF_OPEN`` probe as dispatched (further calls are blocked).

        A no-op unless the breaker is ``HALF_OPEN`` with the probe still available.
        """
        if self.state is CircuitState.HALF_OPEN and not self.probe_used:
            return self.model_copy(update={"probe_used": True})
        return self
