"""Phase-2D Task 9 — evidence sink live broadcast tests (TDD).

The evidence ``sink`` closure in :func:`veridex.competition.service.start_competition`
persists one evidence :class:`~veridex.competition.events.CompetitionEvent` per sealed
``RunEvent``. Prior to this task it never broadcast those events, so a spectator connected
mid-run saw nothing live (only the downstream EXECUTION block broadcast).

This pins the DEC-2D-4 ordering invariant for the evidence path specifically:

  * every persisted evidence event is ALSO broadcast, in the same order (T9-A),
  * for each event, the store append happens strictly BEFORE its broadcast (T9-B) — a
    spectator can never observe an event that is not yet durably persisted,
  * a broadcast callback that raises is swallowed by the existing ``_safe_broadcast``
    helper — it never aborts the run or causes a persisted evidence event to be skipped
    (T9-C).

Fully OFFLINE: in-memory store, deterministic fixture ticks + agents, no network / LLM / DB.
"""

from __future__ import annotations

from tests._arena_fixtures import _beta_agent, _ticks
from veridex.competition.events import CompetitionEvent
from veridex.competition.models import (
    AgentEntry,
    CompetitionConfig,
    CompetitionStatus,
    CompetitionType,
    ExecutionMode,
)
from veridex.competition.service import create_competition, register_agent, start_competition
from veridex.runtime.orchestrator import Agent, deterministic_agent
from veridex.store import InMemoryStore

# ---------------------------------------------------------------------------
# Fixtures / helpers (offline, deterministic — mirrors tests/test_competition_service.py)
# ---------------------------------------------------------------------------


def _agents() -> list[Agent]:
    return [deterministic_agent("agent-alpha"), _beta_agent()]


def _config() -> CompetitionConfig:
    """Minimal valid replay-arena, paper-mode config (paper skips the EXECUTION block)."""
    return CompetitionConfig(
        competition_type=CompetitionType.REPLAY_ARENA,
        source_mode="replay",  # type: ignore[arg-type]
        execution_mode=ExecutionMode.PAPER,
        market_scope="WC:TEST",
        roster_size=2,
    )


async def _seeded_competition(store: InMemoryStore) -> str:
    comp = await create_competition(store, _config())
    for agent in _agents():
        await register_agent(
            store,
            comp.competition_id,
            AgentEntry(
                agent_id=agent.agent_id,
                owner="u",
                strategy="value",
                model="anthropic/claude-sonnet-4",
                proof_mode=agent.proof_mode,
            ),
        )
    return comp.competition_id


class _RecordingStore(InMemoryStore):
    """An ``InMemoryStore`` that also records each event append into a SHARED call log.

    The call log is shared with the recording broadcast fake so tests can assert the
    relative ORDER of append-vs-broadcast calls (append must precede broadcast, per event).
    """

    def __init__(self, call_log: list[tuple[str, int]]) -> None:
        super().__init__()
        self._call_log = call_log

    async def append_competition_events(self, competition_id: str, events: list[CompetitionEvent]) -> None:
        await super().append_competition_events(competition_id, events)
        for event in events:
            self._call_log.append(("append", event.seq))


def _make_recording_broadcast(
    call_log: list[tuple[str, int]],
    *,
    raise_error: bool = False,
) -> tuple[object, list[CompetitionEvent]]:
    """Build a fake broadcast callback that records calls into the SHARED ``call_log``."""
    broadcast_log: list[CompetitionEvent] = []

    async def broadcast(event: CompetitionEvent) -> None:
        call_log.append(("broadcast", event.seq))
        broadcast_log.append(event)
        if raise_error:
            raise RuntimeError("spectator connection died")

    return broadcast, broadcast_log


# ---------------------------------------------------------------------------
# T9-A — every evidence event is broadcast, same order as persisted
# ---------------------------------------------------------------------------


async def test_every_evidence_event_is_broadcast_in_order() -> None:
    call_log: list[tuple[str, int]] = []
    store = _RecordingStore(call_log)
    cid = await _seeded_competition(store)
    broadcast, broadcast_log = _make_recording_broadcast(call_log)

    comp = await start_competition(store, cid, _ticks(), _agents(), broadcast=broadcast)
    assert comp.status == CompetitionStatus.FINALIZED

    persisted = await store.list_competition_events(cid, since_seq=-1)
    persisted_evidence = [e for e in persisted if e.evidence]
    assert persisted_evidence  # the run actually produced evidence events

    broadcast_evidence = [e for e in broadcast_log if e.evidence]
    assert [e.seq for e in broadcast_evidence] == [e.seq for e in persisted_evidence]
    assert [e.payload_hash for e in broadcast_evidence] == [e.payload_hash for e in persisted_evidence]


# ---------------------------------------------------------------------------
# T9-B — persist BEFORE broadcast (DEC-2D-4 ordering invariant)
# ---------------------------------------------------------------------------


async def test_persist_happens_before_broadcast_for_every_evidence_event() -> None:
    call_log: list[tuple[str, int]] = []
    store = _RecordingStore(call_log)
    cid = await _seeded_competition(store)
    broadcast, broadcast_log = _make_recording_broadcast(call_log)

    await start_competition(store, cid, _ticks(), _agents(), broadcast=broadcast)

    evidence_seqs = {e.seq for e in broadcast_log if e.evidence}
    assert evidence_seqs  # sanity: broadcasts actually happened

    for seq in evidence_seqs:
        append_index = call_log.index(("append", seq))
        broadcast_index = call_log.index(("broadcast", seq))
        assert append_index < broadcast_index, f"seq={seq} was broadcast before (or without) being persisted"


# ---------------------------------------------------------------------------
# T9-C — a raising broadcast never aborts the run or skips persistence
# ---------------------------------------------------------------------------


async def test_raising_broadcast_does_not_abort_run_or_skip_persistence() -> None:
    call_log: list[tuple[str, int]] = []
    store = _RecordingStore(call_log)
    cid = await _seeded_competition(store)
    broadcast, broadcast_log = _make_recording_broadcast(call_log, raise_error=True)

    # Must NOT raise: _safe_broadcast swallows the error, the run completes and finalizes.
    comp = await start_competition(store, cid, _ticks(), _agents(), broadcast=broadcast)
    assert comp.status == CompetitionStatus.FINALIZED

    # Every evidence event was attempted (and swallowed) rather than skipped.
    assert broadcast_log

    persisted = await store.list_competition_events(cid, since_seq=-1)
    persisted_evidence = [e for e in persisted if e.evidence]
    # All evidence events are durable even though every broadcast attempt raised.
    assert [e.seq for e in persisted_evidence] == [e.seq for e in broadcast_log if e.evidence]


# ---------------------------------------------------------------------------
# Additive-only — no-broadcast path is byte-identical to today (no broadcast attempted)
# ---------------------------------------------------------------------------


async def test_no_broadcast_callback_means_no_broadcast_attempted() -> None:
    store = InMemoryStore()
    cid = await _seeded_competition(store)

    comp = await start_competition(store, cid, _ticks(), _agents())  # broadcast=None (default)
    assert comp.status == CompetitionStatus.FINALIZED

    persisted = await store.list_competition_events(cid, since_seq=-1)
    assert any(e.evidence for e in persisted)  # the run still persists evidence, just no fanout
