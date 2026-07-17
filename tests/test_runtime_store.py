"""Runtime-events ring-buffer ops store — OPS channel only, never sealed (SEC-003).

The DURABLE OPS spool + its OWNER-SCOPED serving route (I-4) live in ``test_runtime_events_durable``;
this module covers the lightweight in-memory :class:`RuntimeEventStore` (the I-4 cut-fallback).
"""

from __future__ import annotations

from veridex.runtime.runtime_events import RuntimeEventType, runtime_event
from veridex.runtime.runtime_store import RuntimeEventStore


def test_store_records_per_agent_and_filters_since_and_limit() -> None:
    store = RuntimeEventStore()
    sink = store.sink()
    sink(runtime_event(RuntimeEventType.RUN_STARTED, agent_id="a", run_id="r"))
    sink(runtime_event(RuntimeEventType.ACTION_EMITTED, agent_id="a", run_id="r"))
    sink(runtime_event(RuntimeEventType.RUN_STARTED, agent_id="b"))
    a_events = store.list_for_agent("a")
    assert [e.type for e in a_events] == [RuntimeEventType.RUN_STARTED, RuntimeEventType.ACTION_EMITTED]
    assert len(store.list_for_agent("b")) == 1
    assert store.list_for_agent("missing") == []
    # limit returns the most-recent N
    assert len(store.list_for_agent("a", limit=1)) == 1


def test_ring_buffer_evicts_oldest_at_capacity() -> None:
    store = RuntimeEventStore(capacity=2)
    for _ in range(3):
        store.record(runtime_event(RuntimeEventType.LATENCY, agent_id="a"))
    assert len(store.list_for_agent("a")) == 2  # oldest evicted


def test_ops_store_is_separate_from_evidence_seal() -> None:
    """SEC-003 regression: writing ops events never touches a sealed run's evidence_hash."""
    from tests._arena_fixtures import finished_run_result

    run = finished_run_result()
    before = run.evidence_hash
    store = RuntimeEventStore()
    store.record(runtime_event(RuntimeEventType.RUN_STARTED, agent_id="a", run_id=run.run_id))
    store.record(runtime_event(RuntimeEventType.ACTION_EMITTED, agent_id="a", run_id=run.run_id))
    assert run.evidence_hash == before
    # the buffered events carry none of the fields the evidence path requires
    dumped = store.list_for_agent("a")[0].model_dump()
    assert "sequence_no" not in dumped and "evidence" not in dumped and "payload_hash" not in dumped
