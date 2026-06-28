"""Phase-2A Task 4 + Phase-2B Task 5 — Store competition/event + execution-record methods (TDD).

Tests for the 7 competition/event persistence methods (Phase-2A) and the 3 execution-record
persistence methods (Phase-2B Task 5) added to the Store protocol, InMemoryStore, and
PostgresStore. The InMemory path is always run; the Postgres path is gated on DATABASE_URL +
psycopg being present.

Phase-2B Task 5 also covers:
  - REQ-2B-31: InMemoryStore.append_competition_events raises ValueError on duplicate seq.
  - operator_id on CompetitionConfig (default None).
  - _EXECUTION_STATUS_VALUES drift-guard.
"""

from __future__ import annotations

import os

import pytest

from veridex.competition.events import CompetitionEvent, EventType, event_payload_hash
from veridex.competition.models import (
    AgentEntry,
    Competition,
    CompetitionConfig,
    CompetitionStatus,
    CompetitionType,
)
from veridex.execution.models import ExecutionRecord, ExecutionStatus
from veridex.store import (
    _COMPETITION_STATUS_VALUES,
    _EVENT_TYPE_VALUES,
    _EXECUTION_STATUS_VALUES,
    InMemoryStore,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_competition(competition_id: str = "c1", status: CompetitionStatus = CompetitionStatus.DRAFT) -> Competition:
    """Return a minimal valid Competition for testing."""
    return Competition(
        competition_id=competition_id,
        config=CompetitionConfig(
            competition_type=CompetitionType.REPLAY_ARENA,
            source_mode="replay",
            market_scope="WC:FRA-BRA",
            roster_size=2,
        ),
        status=status,
        entries=[],
        run_id=None,
    )


def _make_entry(agent_id: str = "a", owner: str = "u") -> AgentEntry:
    """Return a minimal valid AgentEntry for testing."""
    return AgentEntry(agent_id=agent_id, owner=owner, strategy="value", model=None, proof_mode="reproducible")


def _make_events(count: int = 5) -> list[CompetitionEvent]:
    """Build *count* minimal deterministic CompetitionEvents with seqs 1..count.

    Constructs events directly (no asyncio.run / no network) so the helper is safe
    to call from both sync and async test contexts.
    """
    events = []
    for i in range(1, count + 1):
        payload: dict[str, object] = {"seq": i, "kind": "test_tick"}
        events.append(
            CompetitionEvent(
                competition_id="c_test",
                run_id="run_test",
                seq=i,
                event_type=EventType.MARKET_TICK,
                event_ts=1_000_000 + i,
                evidence=False,
                source_sequence_no=None,
                derived_from=["test"],
                payload=payload,
                payload_hash=event_payload_hash(payload),
            )
        )
    return events


# ---------------------------------------------------------------------------
# Drift-guard tests — tuples must stay in sync with the enums
# ---------------------------------------------------------------------------


def test_competition_status_values_drift_guard() -> None:
    assert tuple(s.value for s in CompetitionStatus) == _COMPETITION_STATUS_VALUES


def test_event_type_values_drift_guard() -> None:
    assert tuple(e.value for e in EventType) == _EVENT_TYPE_VALUES


# ---------------------------------------------------------------------------
# Import hygiene — veridex.store must not eagerly import psycopg
# ---------------------------------------------------------------------------


def test_store_import_does_not_pull_psycopg() -> None:
    # AST check on MODULE-LEVEL imports only: psycopg must stay inside _connect (lazy).
    # sys.modules is unreliable across a test suite (a later gated test may import psycopg);
    # the AST approach mirrors the existing B5-11 pattern in test_orchestrator.py.
    import ast
    from pathlib import Path

    import veridex.store as store_mod

    tree = ast.parse(Path(store_mod.__file__).read_text())
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                assert alias.name.split(".")[0] != "psycopg", "veridex.store eagerly imports psycopg"
        elif isinstance(stmt, ast.ImportFrom) and stmt.level == 0 and stmt.module:
            assert stmt.module.split(".")[0] != "psycopg", "veridex.store eagerly imports from psycopg"


# ---------------------------------------------------------------------------
# InMemoryStore — competition CRUD
# ---------------------------------------------------------------------------


async def test_competition_round_trip() -> None:
    s = InMemoryStore()
    comp = _make_competition("c1")
    await s.create_competition(comp)
    await s.add_agent_entry("c1", _make_entry())
    await s.update_competition_status("c1", CompetitionStatus.OPEN)
    got = await s.get_competition("c1")
    assert got.status is CompetitionStatus.OPEN
    assert len(got.entries) == 1
    assert got.entries[0].agent_id == "a"


async def test_get_unknown_competition_raises() -> None:
    s = InMemoryStore()
    with pytest.raises(KeyError):
        await s.get_competition("missing")


async def test_list_competitions_no_filter() -> None:
    s = InMemoryStore()
    await s.create_competition(_make_competition("c1", CompetitionStatus.DRAFT))
    await s.create_competition(_make_competition("c2", CompetitionStatus.OPEN))
    result = await s.list_competitions()
    assert {c.competition_id for c in result} == {"c1", "c2"}


async def test_list_competitions_filter_by_status() -> None:
    s = InMemoryStore()
    await s.create_competition(_make_competition("c1", CompetitionStatus.DRAFT))
    await s.create_competition(_make_competition("c2", CompetitionStatus.OPEN))
    await s.create_competition(_make_competition("c3", CompetitionStatus.DRAFT))
    result = await s.list_competitions(status=CompetitionStatus.DRAFT)
    assert all(c.status is CompetitionStatus.DRAFT for c in result)
    assert {c.competition_id for c in result} == {"c1", "c3"}


# ---------------------------------------------------------------------------
# InMemoryStore — event append + replay
# ---------------------------------------------------------------------------


async def test_event_append_and_replay_since_seq() -> None:
    # Build 5 events (seqs 1..5); assert that since_seq=2 returns exactly [3, 4, 5].
    s = InMemoryStore()
    await s.create_competition(_make_competition("c1"))
    events = _make_events(5)  # seqs 1..5
    await s.append_competition_events("c1", events)
    result = await s.list_competition_events("c1", since_seq=2)
    assert [e.seq for e in result] == [e.seq for e in events if e.seq > 2]
    # Explicit: ordered ascending
    seqs = [e.seq for e in result]
    assert seqs == sorted(seqs)


async def test_list_competition_events_default_since_seq_zero() -> None:
    """Default since_seq=0 returns all events with seq >= 1."""
    s = InMemoryStore()
    await s.create_competition(_make_competition("c1"))
    events = _make_events(3)  # seqs 1..3 (all > 0)
    await s.append_competition_events("c1", events)
    result = await s.list_competition_events("c1")
    assert len(result) == len(events)
    assert [e.seq for e in result] == sorted(e.seq for e in events)


# ---------------------------------------------------------------------------
# InMemoryStore — isolation (deep-copy contract)
# ---------------------------------------------------------------------------


async def test_inmemory_isolation() -> None:
    """Mutating a returned Competition must not corrupt the stored state."""
    s = InMemoryStore()
    original = _make_competition("c1")
    await s.create_competition(original)
    got = await s.get_competition("c1")
    # Mutate returned copy in two ways
    got.status = CompetitionStatus.OPEN
    got.entries.append(_make_entry())
    # Stored state must remain pristine
    stored = await s.get_competition("c1")
    assert stored.status is CompetitionStatus.DRAFT
    assert stored.entries == []


async def test_inmemory_event_isolation() -> None:
    """Mutating a returned CompetitionEvent's payload must not affect stored state."""
    s = InMemoryStore()
    await s.create_competition(_make_competition("c1"))
    events = _make_events(2)
    await s.append_competition_events("c1", events)
    result = await s.list_competition_events("c1")
    result[0].payload["mutated"] = True
    stored = await s.list_competition_events("c1")
    assert "mutated" not in stored[0].payload


async def test_create_competition_does_not_alias_original() -> None:
    """Mutating the original Competition after create_competition must not corrupt stored state."""
    s = InMemoryStore()
    comp = _make_competition("c1")
    await s.create_competition(comp)
    # Mutate after persisting
    comp.status = CompetitionStatus.OPEN
    stored = await s.get_competition("c1")
    assert stored.status is CompetitionStatus.DRAFT


async def test_create_duplicate_rejected() -> None:
    """create_competition must raise ValueError when the competition_id already exists."""
    s = InMemoryStore()
    await s.create_competition(_make_competition("c1"))
    with pytest.raises(ValueError, match="competition already exists"):
        await s.create_competition(_make_competition("c1"))


async def test_update_competition_run_id_persists_and_is_readable() -> None:
    """A1: update_competition_run_id persists the run_id; get_competition returns it."""
    s = InMemoryStore()
    await s.create_competition(_make_competition("c1"))
    assert (await s.get_competition("c1")).run_id is None
    await s.update_competition_run_id("c1", "run_abc123")
    assert (await s.get_competition("c1")).run_id == "run_abc123"


async def test_update_competition_run_id_unknown_raises_key_error() -> None:
    """A1: update_competition_run_id on an unknown id raises KeyError."""
    s = InMemoryStore()
    with pytest.raises(KeyError):
        await s.update_competition_run_id("missing", "run_xyz")


async def test_event_list_seq0_excluded_by_default() -> None:
    """seq=0 is excluded by the default since_seq=0; since_seq=-1 includes it."""
    s = InMemoryStore()
    await s.create_competition(_make_competition("c1"))
    # Build a log that includes seq=0 (COMPETITION_STARTED) plus seq 1..2.
    payload_0: dict[str, object] = {"seq": 0, "kind": "started"}
    seq0_event = CompetitionEvent(
        competition_id="c_test",
        run_id="run_test",
        seq=0,
        event_type=EventType.COMPETITION_STARTED,
        event_ts=1_000_000,
        evidence=False,
        source_sequence_no=None,
        derived_from=["competition_meta"],
        payload=payload_0,
        payload_hash=event_payload_hash(payload_0),
    )
    events = [seq0_event] + _make_events(2)  # seqs 0, 1, 2
    await s.append_competition_events("c1", events)
    # Default since_seq=0: strict-greater excludes seq=0.
    default_result = await s.list_competition_events("c1")
    assert [e.seq for e in default_result] == [1, 2]
    assert all(e.seq > 0 for e in default_result)
    # since_seq=-1: includes seq=0 (seq > -1 catches all).
    all_result = await s.list_competition_events("c1", since_seq=-1)
    assert [e.seq for e in all_result] == [0, 1, 2]


# ---------------------------------------------------------------------------
# Postgres gated round-trip (skipped unless DATABASE_URL + psycopg present)
# ---------------------------------------------------------------------------


def _psycopg_available() -> bool:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    not (os.getenv("DATABASE_URL") and _psycopg_available()),
    reason="Postgres round-trip: set DATABASE_URL and install psycopg",
)
async def test_postgres_competition_store_round_trip() -> None:
    import psycopg

    from veridex.store import PostgresStore

    dsn = os.environ["DATABASE_URL"]
    store = PostgresStore(dsn=dsn)

    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await store.init_db(conn)

    # Competition CRUD
    comp = _make_competition("pg_c1_task4")
    await store.create_competition(comp)
    await store.add_agent_entry("pg_c1_task4", _make_entry("pg_agent"))
    await store.update_competition_status("pg_c1_task4", CompetitionStatus.OPEN)

    got = await store.get_competition("pg_c1_task4")
    assert got.status is CompetitionStatus.OPEN
    assert len(got.entries) == 1
    assert got.entries[0].agent_id == "pg_agent"

    # Event append + replay
    events = _make_events(5)
    await store.append_competition_events("pg_c1_task4", events)
    result = await store.list_competition_events("pg_c1_task4", since_seq=events[2].seq)
    expected_seqs = [e.seq for e in events if e.seq > events[2].seq]
    assert [e.seq for e in result] == expected_seqs

    # list_competitions filter
    comps = await store.list_competitions(status=CompetitionStatus.OPEN)
    assert any(c.competition_id == "pg_c1_task4" for c in comps)
    draft_comps = await store.list_competitions(status=CompetitionStatus.DRAFT)
    assert not any(c.competition_id == "pg_c1_task4" for c in draft_comps)


# ---------------------------------------------------------------------------
# Phase-2B Task 5 helpers
# ---------------------------------------------------------------------------


def _make_execution_record(
    execution_id: str = "exec_1",
    competition_id: str = "c1",
    status: ExecutionStatus = ExecutionStatus.PROPOSED,
) -> ExecutionRecord:
    """Return a minimal valid ExecutionRecord for testing."""
    return ExecutionRecord(
        execution_id=execution_id,
        competition_id=competition_id,
        run_id="run_test",
        agent_id="agent_1",
        source_sequence_no=1,
        status=status,
        policy_hash="hash_abc",
    )


# ---------------------------------------------------------------------------
# Drift-guard — _EXECUTION_STATUS_VALUES must stay in sync with ExecutionStatus
# ---------------------------------------------------------------------------


def test_execution_status_values_drift_guard() -> None:
    assert tuple(s.value for s in ExecutionStatus) == _EXECUTION_STATUS_VALUES


# ---------------------------------------------------------------------------
# operator_id on CompetitionConfig (REQ-2B-32)
# ---------------------------------------------------------------------------


def test_operator_id_defaults_to_none() -> None:
    """operator_id must default to None when omitted."""
    config = CompetitionConfig(
        competition_type=CompetitionType.REPLAY_ARENA,
        source_mode="replay",
        market_scope="WC:FRA-BRA",
        roster_size=2,
    )
    assert config.operator_id is None


def test_operator_id_can_be_set() -> None:
    """operator_id must be accepted when provided."""
    config = CompetitionConfig(
        competition_type=CompetitionType.REPLAY_ARENA,
        source_mode="replay",
        market_scope="WC:FRA-BRA",
        roster_size=2,
        operator_id="op_abc",
    )
    assert config.operator_id == "op_abc"


# ---------------------------------------------------------------------------
# REQ-2B-31: InMemoryStore.append_competition_events raises on duplicate seq
# ---------------------------------------------------------------------------


async def test_append_competition_events_duplicate_seq_raises_value_error() -> None:
    """REQ-2B-31: duplicate (competition_id, seq) in InMemoryStore raises ValueError."""
    s = InMemoryStore()
    await s.create_competition(_make_competition("c1"))
    events = _make_events(3)  # seqs 1, 2, 3
    await s.append_competition_events("c1", events)
    # Re-append events with overlapping seq values
    duplicate_events = _make_events(2)  # seqs 1, 2 — already present
    with pytest.raises(ValueError, match="seq"):
        await s.append_competition_events("c1", duplicate_events)


async def test_append_competition_events_same_seq_within_batch_raises() -> None:
    """REQ-2B-31: duplicate seq within a single batch raises ValueError atomically.

    The store must reject the ENTIRE batch — no partial append — mirroring Postgres
    transactional all-or-nothing semantics: [seq=1, seq=2, seq=1] must leave the
    bucket completely unchanged even though seq=1 and seq=2 are individually new.
    """
    s = InMemoryStore()
    cid = "c1"
    await s.create_competition(_make_competition(cid))
    events = _make_events(2)  # seqs 1, 2
    # Batch: [seq=1, seq=2, seq=1] — first two are new; third repeats seq=1.
    dup_batch = [events[0], events[1], events[0]]
    with pytest.raises(ValueError, match="seq"):
        await s.append_competition_events(cid, dup_batch)
    # Atomicity: bucket must be entirely unchanged — no partial write.
    assert await s.list_competition_events(cid, since_seq=-1) == []


# ---------------------------------------------------------------------------
# InMemoryStore — execution record round-trip
# ---------------------------------------------------------------------------


async def test_execution_record_round_trip_inmemory() -> None:
    """append_execution_record → get_execution_record returns equal record; list_executions returns it."""
    s = InMemoryStore()
    await s.create_competition(_make_competition("c1"))
    rec = _make_execution_record("exec_1", "c1")
    await s.append_execution_record(rec)
    got = await s.get_execution_record("exec_1")
    assert got == rec
    listed = await s.list_executions("c1")
    assert len(listed) == 1
    assert listed[0].execution_id == "exec_1"


async def test_get_unknown_execution_raises_key_error() -> None:
    """get_execution_record on an unknown id raises KeyError."""
    s = InMemoryStore()
    with pytest.raises(KeyError):
        await s.get_execution_record("missing")


async def test_execution_record_idempotent_upsert() -> None:
    """Upserting the same execution_id with an advanced status → latest stored; no duplication."""
    s = InMemoryStore()
    await s.create_competition(_make_competition("c1"))
    rec1 = _make_execution_record("exec_1", "c1", ExecutionStatus.PROPOSED)
    await s.append_execution_record(rec1)
    rec2 = _make_execution_record("exec_1", "c1", ExecutionStatus.LAW_APPROVED)
    await s.append_execution_record(rec2)
    got = await s.get_execution_record("exec_1")
    assert got.status is ExecutionStatus.LAW_APPROVED
    listed = await s.list_executions("c1")
    assert len(listed) == 1  # no duplication


async def test_execution_record_deep_copy_isolation() -> None:
    """Mutating a returned ExecutionRecord must not corrupt the stored state."""
    s = InMemoryStore()
    await s.create_competition(_make_competition("c1"))
    rec = _make_execution_record("exec_1", "c1", ExecutionStatus.PROPOSED)
    await s.append_execution_record(rec)
    got = await s.get_execution_record("exec_1")
    # Mutate the returned copy
    got.status = ExecutionStatus.LAW_APPROVED
    # Stored state must remain PROPOSED
    stored = await s.get_execution_record("exec_1")
    assert stored.status is ExecutionStatus.PROPOSED


async def test_list_executions_deterministic_order() -> None:
    """list_executions returns records sorted by execution_id."""
    s = InMemoryStore()
    await s.create_competition(_make_competition("c1"))
    await s.append_execution_record(_make_execution_record("exec_z", "c1"))
    await s.append_execution_record(_make_execution_record("exec_a", "c1"))
    await s.append_execution_record(_make_execution_record("exec_m", "c1"))
    listed = await s.list_executions("c1")
    ids = [r.execution_id for r in listed]
    assert ids == sorted(ids)


async def test_list_executions_filters_by_competition_id() -> None:
    """list_executions only returns records for the requested competition."""
    s = InMemoryStore()
    await s.create_competition(_make_competition("c1"))
    await s.create_competition(_make_competition("c2"))
    await s.append_execution_record(_make_execution_record("exec_c1", "c1"))
    await s.append_execution_record(_make_execution_record("exec_c2", "c2"))
    listed_c1 = await s.list_executions("c1")
    assert all(r.competition_id == "c1" for r in listed_c1)
    assert len(listed_c1) == 1
    listed_c2 = await s.list_executions("c2")
    assert all(r.competition_id == "c2" for r in listed_c2)
    assert len(listed_c2) == 1


async def test_write_original_not_aliased_in_execution_record() -> None:
    """Mutating the original ExecutionRecord after append must not corrupt stored state."""
    s = InMemoryStore()
    await s.create_competition(_make_competition("c1"))
    rec = _make_execution_record("exec_1", "c1", ExecutionStatus.PROPOSED)
    await s.append_execution_record(rec)
    # Mutate the original after persisting
    rec.status = ExecutionStatus.LAW_APPROVED
    stored = await s.get_execution_record("exec_1")
    assert stored.status is ExecutionStatus.PROPOSED


# ---------------------------------------------------------------------------
# Postgres gated — execution record round-trip
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (os.getenv("DATABASE_URL") and _psycopg_available()),
    reason="Postgres round-trip: set DATABASE_URL and install psycopg",
)
async def test_postgres_execution_store_round_trip() -> None:
    import psycopg

    from veridex.store import PostgresStore

    dsn = os.environ["DATABASE_URL"]
    store = PostgresStore(dsn=dsn)

    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await store.init_db(conn)

    comp = _make_competition("pg_c1_task5")
    await store.create_competition(comp)

    # Basic round-trip
    rec = _make_execution_record("pg_exec_1", "pg_c1_task5", ExecutionStatus.PROPOSED)
    await store.append_execution_record(rec)
    got = await store.get_execution_record("pg_exec_1")
    assert got == rec

    # Idempotent upsert — advanced status stored; list has ONE entry
    rec2 = _make_execution_record("pg_exec_1", "pg_c1_task5", ExecutionStatus.LAW_APPROVED)
    await store.append_execution_record(rec2)
    got2 = await store.get_execution_record("pg_exec_1")
    assert got2.status is ExecutionStatus.LAW_APPROVED
    listed = await store.list_executions("pg_c1_task5")
    assert len(listed) == 1
    assert listed[0].execution_id == "pg_exec_1"

    # Unknown id raises KeyError
    with pytest.raises(KeyError):
        await store.get_execution_record("pg_exec_missing")
