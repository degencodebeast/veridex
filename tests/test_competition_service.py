"""Phase-2A Task 5 — competition service + orchestrator event_sink (KEYSTONE) tests (TDD).

These tests pin the CORE TRUST INVARIANT: the live spectator stream is a PROJECTION of the
sealed proof record, never a second source of truth. Concretely:

  * ``run_competition``'s ``event_sink`` only ever receives ``RunEvent``-validated dicts, in
    ``sequence_no`` order, and adding a sink does NOT change the sealed ``evidence_hash``.
  * ``start_competition`` persists a live evidence prefix that is byte-equivalent (canonical
    fields) to ``build_event_log(final_run_result)``'s evidence prefix (AC-213), then appends
    ONLY the derived tail (no evidence duplication / no ``UNIQUE(competition_id, seq)`` hit).
  * Idempotency + Phase-2A guards: double-finalize and non-paper start both reject with stable
    reason strings; ``config_hash`` is pinned and ``proof_mode`` normalized at registration.

Fully OFFLINE: in-memory store, deterministic fixture ticks + agents, no network / LLM / DB.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests._arena_fixtures import _beta_agent, _ticks
from veridex.competition.events import CompetitionEvent, EventType, build_event_log, event_payload_hash
from veridex.competition.models import (
    AgentEntry,
    CompetitionConfig,
    CompetitionStatus,
    CompetitionType,
    ExecutionMode,
)
from veridex.competition.service import (
    _PREFIX_MISMATCH,
    CompetitionConflictError,
    CompetitionIntegrityError,
    _assert_prefix_parity,
    create_competition,
    register_agent,
    start_competition,
)
from veridex.ingest.marketstate import MarketState
from veridex.runtime.orchestrator import Agent, deterministic_agent, run_competition
from veridex.store import InMemoryStore

# ---------------------------------------------------------------------------
# Fixtures / helpers (offline, deterministic)
# ---------------------------------------------------------------------------


def _agents() -> list[Agent]:
    """Two deterministic, offline agents: the reproducible baseline + a verified-mode agent."""
    return [deterministic_agent("agent-alpha"), _beta_agent()]


def _config(
    *,
    execution_mode: ExecutionMode = ExecutionMode.PAPER,
    source_mode: str = "replay",
) -> CompetitionConfig:
    """Build a minimal valid replay-arena competition config."""
    return CompetitionConfig(
        competition_type=CompetitionType.REPLAY_ARENA,
        source_mode=source_mode,  # type: ignore[arg-type]
        execution_mode=execution_mode,
        market_scope="WC:TEST",
        roster_size=2,
    )


async def _seeded_competition(
    store: InMemoryStore,
    *,
    execution_mode: ExecutionMode = ExecutionMode.PAPER,
) -> str:
    """Create a competition and register the two fixture agents; return its id."""
    comp = await create_competition(store, _config(execution_mode=execution_mode))
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


# ---------------------------------------------------------------------------
# Part 1 — orchestrator event_sink (watchpoint #1)
# ---------------------------------------------------------------------------


async def test_orchestrator_event_sink_receives_validated_events() -> None:
    """The sink only ever sees RunEvent-validated dicts, in seq order; seal is unchanged."""
    seen: list[dict[str, Any]] = []

    async def sink(ev: dict[str, Any]) -> None:
        seen.append(ev)

    with_sink = await run_competition(_ticks(), _agents(), source_mode="replay", event_sink=sink)
    without_sink = await run_competition(_ticks(), _agents(), source_mode="replay")

    # every received item has the RunEvent shape ...
    assert seen
    assert all("sequence_no" in e and "event_type" in e for e in seen)
    assert all(isinstance(e["sequence_no"], int) for e in seen)
    # ... arrives in sequence order ...
    assert [e["sequence_no"] for e in seen] == sorted(e["sequence_no"] for e in seen)
    # ... and matches exactly the sealed run_events of an identical no-sink run.
    assert seen == without_sink.run_events
    # the deterministic seal is byte-unchanged by the additive sink.
    assert with_sink.evidence_hash == without_sink.evidence_hash


# ---------------------------------------------------------------------------
# Part 2 — service: registration, gates, finalize parity
# ---------------------------------------------------------------------------


async def test_config_hash_pinned_and_proof_mode_normalized() -> None:  # AC-202/209/216
    store = InMemoryStore()
    comp = await create_competition(store, _config())
    entry = await register_agent(
        store,
        comp.competition_id,
        AgentEntry(
            agent_id="a",
            owner="u",
            strategy="value",
            model="anthropic/claude-sonnet-4",
            proof_mode="LLM/evidence-verified",
        ),
    )
    assert entry.config_hash and entry.proof_mode == "verified"

    # the pinned entry is what got persisted on the competition roster.
    reloaded = await store.get_competition(comp.competition_id)
    assert reloaded.entries[-1].config_hash == entry.config_hash
    assert reloaded.entries[-1].proof_mode == "verified"


async def test_config_hash_excludes_config_hash_and_eligibility() -> None:  # CON-207
    import hashlib

    from veridex.runtime.evidence import serialize_payload

    store = InMemoryStore()
    comp = await create_competition(store, _config())
    entry = await register_agent(
        store,
        comp.competition_id,
        AgentEntry(
            agent_id="a",
            owner="u",
            strategy="value",
            model="anthropic/claude-sonnet-4",
            proof_mode="LLM/evidence-verified",
            config_hash="SHOULD_BE_IGNORED",
            execution_eligibility=True,
        ),
    )
    expected = hashlib.sha256(
        serialize_payload(
            {
                "agent_id": "a",
                "strategy": "value",
                "model": "anthropic/claude-sonnet-4",
                "proof_mode": "verified",
            }
        ).encode("utf-8")
    ).hexdigest()
    assert entry.config_hash == expected


async def test_non_paper_start_rejected() -> None:  # AC-217
    store = InMemoryStore()
    cid = await _seeded_competition(store, execution_mode=ExecutionMode.LIVE_GUARDED)
    with pytest.raises(ValueError, match="execution_mode_not_available_in_phase_2a"):
        await start_competition(store, cid, _ticks(), _agents())
    # no run / no events created on the rejected start.
    assert await store.list_competition_events(cid, since_seq=-1) == []


async def test_live_evidence_prefix_equals_final_projection() -> None:  # AC-213 KEYSTONE
    store = InMemoryStore()
    cid = await _seeded_competition(store)

    comp = await start_competition(store, cid, _ticks(), _agents())
    assert comp.run_id is not None

    # Reproduce the sealed run deterministically (same inputs + run_id) to obtain the projection.
    expected_run = await run_competition(_ticks(), _agents(), source_mode="replay", run_id=comp.run_id)
    base_ts = _ticks()[0].ts
    expected_log = build_event_log(
        expected_run,
        {"competition_id": cid, "anchor_status": "not_anchored", "event_ts": base_ts},
    )
    n = len(expected_run.run_events)
    expected_prefix = [e for e in expected_log if e.seq <= n]  # seq 0..N

    persisted = await store.list_competition_events(cid, since_seq=-1)  # include seq0
    persisted_prefix = [e for e in persisted if e.seq <= n]

    # byte-equivalent (canonical fields) live prefix == projection prefix.
    assert [e.canonical_dict() for e in persisted_prefix] == [e.canonical_dict() for e in expected_prefix]

    # and specifically the evidence events line up one-for-one.
    persisted_evidence = [e for e in persisted if e.evidence]
    expected_evidence = [e for e in expected_log if e.evidence]
    assert [e.canonical_dict() for e in persisted_evidence] == [e.canonical_dict() for e in expected_evidence]


async def test_double_finalize_rejected() -> None:  # idempotency gate
    store = InMemoryStore()
    cid = await _seeded_competition(store)
    await start_competition(store, cid, _ticks(), _agents())
    with pytest.raises(ValueError, match="competition_already_finalized"):
        await start_competition(store, cid, _ticks(), _agents())


async def test_finalize_appends_only_derived_no_evidence_dupe() -> None:  # the Major-fix guard
    store = InMemoryStore()
    cid = await _seeded_competition(store)
    await start_competition(store, cid, _ticks(), _agents())

    log = await store.list_competition_events(cid, since_seq=-1)
    seqs = [e.seq for e in log]
    assert len(seqs) == len(set(seqs))  # no duplicate seq
    assert seqs == sorted(seqs)
    assert log[0].seq == 0 and log[0].event_type == EventType.COMPETITION_STARTED

    evidence = [e for e in log if e.evidence]
    assert [e.seq for e in evidence] == list(range(1, len(evidence) + 1))  # contiguous 1..N, once
    # everything beyond the evidence prefix is derived tail only.
    assert all(not e.evidence for e in log if e.seq > len(evidence))
    # the tail ends with COMPETITION_FINALIZED preceded by PROOF_ANCHOR.
    assert log[-1].event_type == EventType.COMPETITION_FINALIZED
    assert log[-2].event_type == EventType.PROOF_ANCHOR


async def test_finalized_status_persisted() -> None:
    store = InMemoryStore()
    cid = await _seeded_competition(store)
    comp = await start_competition(store, cid, _ticks(), _agents())
    assert comp.status == CompetitionStatus.FINALIZED
    reloaded = await store.get_competition(cid)
    assert reloaded.status == CompetitionStatus.FINALIZED


async def test_start_missing_marketstates_typing() -> None:
    """A paper competition with a single-tick run still produces a contiguous projected log."""
    store = InMemoryStore()
    cid = await _seeded_competition(store)
    single: list[MarketState] = _ticks()[:1]
    comp = await start_competition(store, cid, single, _agents())
    log = await store.list_competition_events(cid, since_seq=-1)
    assert comp.status == CompetitionStatus.FINALIZED
    assert [e.seq for e in log] == list(range(len(log)))  # 0..end contiguous


# ---------------------------------------------------------------------------
# Part A follow-ups
# ---------------------------------------------------------------------------


async def test_run_id_persisted_in_store_after_start() -> None:
    """A1: After start_competition, store.get_competition().run_id equals comp.run_id."""
    store = InMemoryStore()
    cid = await _seeded_competition(store)
    comp = await start_competition(store, cid, _ticks(), _agents())
    assert comp.run_id is not None
    stored = await store.get_competition(cid)
    assert stored.run_id == comp.run_id


async def test_start_running_rejected() -> None:
    """A2: start_competition raises competition_already_running when status is RUNNING."""
    from veridex.competition.service import _ALREADY_RUNNING

    store = InMemoryStore()
    cid = await _seeded_competition(store)
    # Simulate an interrupted start: manually advance status to RUNNING.
    await store.update_competition_status(cid, CompetitionStatus.RUNNING)
    with pytest.raises(ValueError, match=_ALREADY_RUNNING):
        await start_competition(store, cid, _ticks(), _agents())
    # No events should have been created during the rejected call.
    events_after = await store.list_competition_events(cid, since_seq=-1)
    assert events_after == []


async def test_empty_marketstates_finalizes_cleanly() -> None:
    """A4: start_competition with marketstates=[] produces seq=0 + derived tail, no evidence."""
    from veridex.competition.events import EventType

    store = InMemoryStore()
    cid = await _seeded_competition(store)
    comp = await start_competition(store, cid, [], _agents())
    assert comp.status == CompetitionStatus.FINALIZED

    log = await store.list_competition_events(cid, since_seq=-1)
    # seq=0 is always COMPETITION_STARTED (prefix_end=0 → zero evidence events).
    assert log[0].seq == 0
    assert log[0].event_type == EventType.COMPETITION_STARTED
    # With no marketstates there are no evidence events at all.
    evidence = [e for e in log if e.evidence]
    assert evidence == []
    # The log still ends with the full derived tail.
    assert log[-1].event_type == EventType.COMPETITION_FINALIZED
    assert any(e.event_type == EventType.PROOF_ANCHOR for e in log)


# ---------------------------------------------------------------------------
# A3 — _assert_prefix_parity diagnostic (unit-tested directly; only path to
# exercise the safety check since live≡projection is guaranteed by construction)
# ---------------------------------------------------------------------------


def _make_test_event(seq: int, competition_id: str = "c_test", run_id: str = "run_test") -> CompetitionEvent:
    """Build a minimal deterministic CompetitionEvent for parity tests."""
    payload = {"seq": seq, "value": "v"}
    return CompetitionEvent(
        competition_id=competition_id,
        run_id=run_id,
        seq=seq,
        event_type=EventType.MARKET_TICK,
        event_ts=1_000_000 + seq,
        evidence=False,
        source_sequence_no=None,
        derived_from=["test"],
        payload=payload,
        payload_hash=event_payload_hash(payload),
    )


def test_assert_prefix_parity_length_mismatch_raises_integrity_error_with_seq() -> None:
    """A3: length mismatch raises CompetitionIntegrityError with stable prefix + seq= in message."""
    persisted = [_make_test_event(0), _make_test_event(1)]
    projection = [_make_test_event(0), _make_test_event(1), _make_test_event(2)]
    with pytest.raises(CompetitionIntegrityError, match=_PREFIX_MISMATCH) as exc_info:
        _assert_prefix_parity(persisted, projection)
    msg = str(exc_info.value)
    assert "seq=2" in msg  # first missing seq = min(2, 3) = 2
    assert "length mismatch" in msg


def test_assert_prefix_parity_field_mismatch_raises_integrity_error_with_seq() -> None:
    """A3: field mismatch raises CompetitionIntegrityError with stable prefix + divergent seq= in message."""
    good = _make_test_event(0)
    divergent = _make_test_event(1)
    # Build a projection event at seq=1 with a different payload so canonical_dict differs.
    bad_payload = {"seq": 1, "value": "TAMPERED"}
    divergent_projected = CompetitionEvent(
        competition_id=divergent.competition_id,
        run_id=divergent.run_id,
        seq=1,
        event_type=EventType.MARKET_TICK,
        event_ts=divergent.event_ts,
        evidence=False,
        source_sequence_no=None,
        derived_from=["test"],
        payload=bad_payload,
        payload_hash=event_payload_hash(bad_payload),
    )
    with pytest.raises(CompetitionIntegrityError, match=_PREFIX_MISMATCH) as exc_info:
        _assert_prefix_parity([good, divergent], [good, divergent_projected])
    msg = str(exc_info.value)
    assert "seq=1" in msg
    assert "field mismatch" in msg


def test_assert_prefix_parity_identical_lists_passes() -> None:
    """_assert_prefix_parity does NOT raise when both lists are byte-identical."""
    events = [_make_test_event(i) for i in range(3)]
    _assert_prefix_parity(events, events)  # must not raise


def test_conflict_gates_raise_competition_conflict_error() -> None:
    """CompetitionConflictError is a ValueError (existing match tests remain valid)."""
    err = CompetitionConflictError("competition_already_finalized")
    assert isinstance(err, ValueError)
    assert str(err) == "competition_already_finalized"
