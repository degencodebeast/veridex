"""§4.4 AgentRuntime seam + AgnoRuntime required-tier synthesis (SEC-010, offline)."""
from __future__ import annotations

import pytest

from veridex.runtime.runtime_events import REQUIRED_RUNTIME_EVENTS, RuntimeEvent, RuntimeEventType
from veridex.runtime.runtime_protocol import AgentRuntime, AgnoRuntime
from veridex.runtime.schemas import AgentAction, SportsActionType


def _sink() -> tuple[list[RuntimeEvent], object]:
    events: list[RuntimeEvent] = []
    return events, events.append


async def test_required_tier_emitted_on_happy_path() -> None:
    events, sink = _sink()

    async def fake_propose(ms, **kw):
        return AgentAction(type=SportsActionType.WAIT)

    rt = AgnoRuntime(sink=sink, propose_fn=fake_propose)
    rt.run_started(agent_id="a", run_id="r")
    action = await rt.propose_action({"tick_seq": 0}, agent_id="a", run_id="r")
    rt.run_completed(agent_id="a", run_id="r")
    assert action.type is SportsActionType.WAIT
    # A successful run emits every required event except the failure terminal (RUN_FAILED),
    # which only fires on the failure path (covered by test_failure_emits_invalid_schema_then_run_failed).
    assert REQUIRED_RUNTIME_EVENTS - {RuntimeEventType.RUN_FAILED} <= {e.type for e in events}
    assert RuntimeEventType.RUN_COMPLETED in {e.type for e in events}
    assert all(e.channel == "OPS" for e in events)


async def test_schema_validation_event_marks_valid() -> None:
    events, sink = _sink()

    async def fake_propose(ms, **kw):
        return AgentAction(type=SportsActionType.FLAG_VALUE, params={"market_key": "m", "side": "over"})

    await AgnoRuntime(sink=sink, propose_fn=fake_propose).propose_action({}, agent_id="a")
    sv = [e for e in events if e.type is RuntimeEventType.SCHEMA_VALIDATION]
    assert sv and sv[0].payload["valid"] is True


async def test_failure_emits_invalid_schema_then_run_failed() -> None:
    events, sink = _sink()

    async def boom(ms, **kw):
        raise ValueError("model exploded")

    rt = AgnoRuntime(sink=sink, propose_fn=boom)
    with pytest.raises(ValueError):
        await rt.propose_action({}, agent_id="a", run_id="r")
    rt.run_failed(agent_id="a", run_id="r", error="model exploded")
    sv = [e for e in events if e.type is RuntimeEventType.SCHEMA_VALIDATION]
    assert sv and sv[0].payload["valid"] is False
    assert RuntimeEventType.RUN_FAILED in {e.type for e in events}


async def test_byoa_runtime_satisfies_protocol_and_required_tier() -> None:
    """A bare BYOA proposer (no model telemetry) still produces the required tier (REQ-031)."""
    events, sink = _sink()

    async def byoa(ms, **kw):
        return AgentAction(type=SportsActionType.WAIT)

    rt = AgnoRuntime(sink=sink, propose_fn=byoa)
    assert isinstance(rt, AgentRuntime)  # structural Protocol check (runtime_checkable)
    rt.run_started(agent_id="byoa")
    await rt.propose_action({}, agent_id="byoa")
    rt.run_completed(agent_id="byoa")
    # Required tier minus the failure terminal — this is the happy BYOA path (REQ-031).
    assert REQUIRED_RUNTIME_EVENTS - {RuntimeEventType.RUN_FAILED} <= {e.type for e in events}
