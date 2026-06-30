"""§4.4 RuntimeEvent — runtime-neutral OPS-channel telemetry, never sealed (SEC-003)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from veridex.runtime.runtime_events import (
    REQUIRED_RUNTIME_EVENTS,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeStatus,
    runtime_event,
)
from veridex.runtime.schemas import RunEvent


def test_required_tier_is_exactly_the_six() -> None:
    assert {e.value for e in REQUIRED_RUNTIME_EVENTS} == {
        "run_started",
        "status_changed",
        "action_emitted",
        "schema_validation",
        "run_completed",
        "run_failed",
    }


def test_runtime_event_is_ops_channel() -> None:
    ev = runtime_event(RuntimeEventType.STATUS_CHANGED, agent_id="a", status=RuntimeStatus.RUNNING.value)
    assert ev.channel == "OPS" and ev.payload["status"] == "running"


def test_runtime_event_has_no_seal_fields() -> None:
    """SEC-003: structurally cannot carry the fields the evidence path requires."""
    d = runtime_event(RuntimeEventType.ACTION_EMITTED, agent_id="a").model_dump()
    assert "sequence_no" not in d and "evidence" not in d and "payload_hash" not in d


def test_runtime_event_rejected_by_runevent_schema() -> None:
    """A RuntimeEvent dict can never be validated as a sealed RunEvent (no sequence_no)."""
    d = runtime_event(RuntimeEventType.RUN_STARTED, agent_id="a").model_dump()
    with pytest.raises(ValidationError):
        RunEvent.model_validate(d)


def test_emitting_runtime_events_never_touches_evidence_hash() -> None:
    from tests._arena_fixtures import finished_run_result

    run = finished_run_result()
    before = run.evidence_hash
    collected: list[RuntimeEvent] = []
    sink = collected.append  # an OPS sink — distinct from the orchestrator's evidence event_sink
    sink(runtime_event(RuntimeEventType.RUN_STARTED, agent_id="a", run_id=run.run_id))
    sink(runtime_event(RuntimeEventType.RUN_COMPLETED, agent_id="a", run_id=run.run_id))
    assert run.evidence_hash == before
    assert len(collected) == 2 and all(isinstance(e, RuntimeEvent) for e in collected)
