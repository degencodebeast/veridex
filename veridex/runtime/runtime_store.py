"""In-memory OPS-channel buffer for RuntimeEvents (Agent Ops drawer source).

A per-``agent_id`` ring buffer the ``RuntimeEventSink`` (Task 15) writes to. It is the OPS
channel ONLY: there is no code path from here into ``compute_evidence_hash`` or the canonical
competition event log, so SEC-003 holds by construction. Lossy by design (bounded ``capacity``);
the proof record is the canonical log, never this buffer.
"""

from __future__ import annotations

from collections import deque

from veridex.runtime.runtime_events import RuntimeEvent, RuntimeEventSink


class RuntimeEventStore:
    """A bounded, per-agent in-memory buffer of OPS-channel RuntimeEvents.

    Attributes:
        capacity: Max events retained per ``agent_id`` (oldest evicted past this).
    """

    def __init__(self, *, capacity: int = 2000) -> None:
        self._capacity = capacity
        self._by_agent: dict[str, deque[RuntimeEvent]] = {}

    def record(self, event: RuntimeEvent) -> None:
        """Append ``event`` to its agent's ring buffer (this IS the ``RuntimeEventSink``)."""
        buf = self._by_agent.get(event.agent_id)
        if buf is None:
            buf = deque(maxlen=self._capacity)
            self._by_agent[event.agent_id] = buf
        buf.append(event)

    def sink(self) -> RuntimeEventSink:
        """Return the ``record`` callable as a ``RuntimeEventSink`` (wire into an AgnoRuntime)."""
        return self.record

    def list_for_agent(self, agent_id: str, *, since: int = 0, limit: int | None = None) -> list[RuntimeEvent]:
        """Return one agent's buffered events with ``ts >= since`` (oldest→newest), last ``limit``.

        Args:
            agent_id: The agent whose telemetry to read.
            since: ``ts`` lower bound (ms); ``0`` returns everything retained.
            limit: When set, return only the most-recent ``limit`` matching events.

        Returns:
            The matching :class:`~veridex.runtime.runtime_events.RuntimeEvent` list.
        """
        events = [e for e in self._by_agent.get(agent_id, ()) if e.ts >= since]
        if limit is not None:
            events = events[-limit:]
        return events
