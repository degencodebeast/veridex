"""Phase-2A Task 4 — Store competition/event methods (TDD).

Tests for the 7 new competition/event persistence methods added to the Store protocol,
InMemoryStore, and PostgresStore. The InMemory path is always run; the Postgres path is
gated on DATABASE_URL + psycopg being present.
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
from veridex.store import (
    _COMPETITION_STATUS_VALUES,
    _EVENT_TYPE_VALUES,
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
