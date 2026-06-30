"""§4.4 RuntimeEvent — runtime-neutral OPS-channel telemetry (SEC-003 / SEC-010).

NON-deterministic operational telemetry (model calls, latency, tokens, status,
schema-validation). It MUST NEVER be hashed into evidence, sealed, scored, ranked, or appended to
the canonical competition event log (SEC-003). To make that structurally impossible, a
RuntimeEvent carries NO ``sequence_no``, NO ``evidence`` flag, and NO ``payload_hash`` — the three
fields the evidence/competition-event path requires — so it cannot masquerade as a RunEvent or
CompetitionEvent.

Runtime-neutral (SEC-010): any runtime (Agno now; Hermes/BYOA/deterministic later) feeds the same
contract via its adapter (see ``veridex.runtime.runtime_protocol``). The Logs-tab source tag
(OPS/PROOF/POLICY/EXEC) is applied by the UI; only OPS telemetry lives here.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class RuntimeEventType(str, Enum):
    """The §4.4 runtime telemetry vocabulary (required tier + optional enrichment)."""

    # --- required tier: every runtime (incl. a bare BYOA endpoint) MUST emit these ---
    RUN_STARTED = "run_started"
    STATUS_CHANGED = "status_changed"
    ACTION_EMITTED = "action_emitted"
    SCHEMA_VALIDATION = "schema_validation"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    # --- optional enrichment: best-effort; absent fields render "—" in the drawer (REQ-031) ---
    MODEL_CALL_STARTED = "model_call_started"
    MODEL_CALL_COMPLETED = "model_call_completed"
    TOKEN_USAGE = "token_usage"
    LATENCY = "latency"
    TOOL_CALL = "tool_call"
    RETRY = "retry"
    ERROR = "error"
    TRACE_LINK = "trace_link"


#: The required tier — the Agent Ops drawer Overview tab binds to exactly these (REQ-030).
REQUIRED_RUNTIME_EVENTS: frozenset[RuntimeEventType] = frozenset(
    {
        RuntimeEventType.RUN_STARTED,
        RuntimeEventType.STATUS_CHANGED,
        RuntimeEventType.ACTION_EMITTED,
        RuntimeEventType.SCHEMA_VALIDATION,
        RuntimeEventType.RUN_COMPLETED,
        RuntimeEventType.RUN_FAILED,
    }
)


class RuntimeStatus(str, Enum):
    """Lifecycle status carried in a STATUS_CHANGED payload (Overview tab)."""

    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETED = "completed"


class RuntimeEvent(BaseModel):
    """One OPS-channel telemetry event. NEVER sealed/hashed/scored/ranked (SEC-003).

    Attributes:
        type: The event type (required or optional tier).
        agent_id: The agent this telemetry is about.
        run_id: Correlation id for the run, when known.
        session_id: Runtime session id, when known.
        ts: Wall-clock emit time (ms). NON-deterministic — exactly why it must never be sealed.
        channel: Always ``"OPS"``. PROOF/POLICY/EXEC streams live on the competition event log.
        payload: Free-form, secret-free telemetry detail.
    """

    type: RuntimeEventType
    agent_id: str
    run_id: str | None = None
    session_id: str | None = None
    ts: int = Field(default_factory=lambda: int(time.time() * 1000))
    channel: Literal["OPS"] = "OPS"
    payload: dict[str, Any] = Field(default_factory=dict)


#: An ops-channel sink: ``(event) -> None``. DISTINCT from the orchestrator's evidence
#: ``event_sink`` (SEALED RunEvents) — wiring a RuntimeEventSink never feeds the deterministic seal.
RuntimeEventSink = Callable[[RuntimeEvent], None]


def runtime_event(
    event_type: RuntimeEventType,
    *,
    agent_id: str,
    run_id: str | None = None,
    session_id: str | None = None,
    **payload: Any,
) -> RuntimeEvent:
    """Convenience constructor: ``runtime_event(ACTION_EMITTED, agent_id="a", action="WAIT")``."""
    return RuntimeEvent(type=event_type, agent_id=agent_id, run_id=run_id, session_id=session_id, payload=payload)
