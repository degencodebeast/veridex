"""Competition service — Phase 2A Task 5 (the finalize-parity keystone).

This async SHELL ties the competition aggregate to the sealed Phase-1 run. Its load-bearing
job is the CORE TRUST INVARIANT: the live spectator stream is a *projection* of the sealed
proof record, never a second source of truth.

Concretely, :func:`start_competition`:

  1. persists the canonical seq=0 ``COMPETITION_STARTED`` event BEFORE the run,
  2. runs ``run_competition`` with a STATEFUL live sink that persists one evidence event per
     sealed ``RunEvent`` (at ``seq = sequence_no + 1``, in order), and
  3. at finalize, projects the sealed ``RunResult`` via :func:`build_event_log`, VERIFIES the
     live-persisted evidence prefix (seq 0..N) is byte-equivalent to that projection's prefix,
     then appends ONLY the derived tail (seq > N) — never re-appending evidence events.

The seq=0 event and each evidence event are built by the SHARED constructors in
:mod:`veridex.competition.events` (``build_competition_started_event`` /
``build_evidence_event``) that :func:`build_event_log` also uses, so live ≡ projection by
construction; the finalize check is the belt-and-braces guard.

CON-207: ``config_hash`` is pinned via the ONE canonical serializer
(:func:`veridex.runtime.evidence.serialize_payload`), excluding ``config_hash`` and
``execution_eligibility`` from the hashed input. ``proof_mode`` is normalized to the two
canonical Phase-2A values via :func:`veridex.competition.models.normalize_proof_mode`.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from veridex.competition.events import (
    CompetitionEvent,
    build_competition_started_event,
    build_event_log,
    build_evidence_event,
)
from veridex.competition.models import (
    AgentEntry,
    Competition,
    CompetitionConfig,
    CompetitionStatus,
    ExecutionMode,
    normalize_proof_mode,
)
from veridex.runtime.evidence import serialize_payload
from veridex.runtime.orchestrator import run_competition

if TYPE_CHECKING:  # the heavy/offline-safe types are only needed for annotations
    from veridex.ingest.marketstate import MarketState
    from veridex.runtime.orchestrator import Agent
    from veridex.store import Store

# Stable reason strings (tests match on these EXACTLY — do not edit casually).
_ALREADY_FINALIZED = "competition_already_finalized"
_EXECUTION_MODE_UNAVAILABLE = "execution_mode_not_available_in_phase_2a"
_PREFIX_MISMATCH = "live_evidence_prefix_diverged_from_projection"


async def create_competition(store: Store, config: CompetitionConfig) -> Competition:
    """Create and persist a new ``DRAFT`` competition with an empty roster.

    Args:
        store: The async repository.
        config: The immutable competition configuration snapshot.

    Returns:
        The persisted :class:`~veridex.competition.models.Competition` (status ``DRAFT``,
        empty ``entries``, ``run_id`` ``None``).
    """
    competition = Competition(
        competition_id=f"c_{uuid4().hex}",
        config=config,
        status=CompetitionStatus.DRAFT,
        entries=[],
        run_id=None,
    )
    await store.create_competition(competition)
    return competition


async def register_agent(store: Store, competition_id: str, entry: AgentEntry) -> AgentEntry:
    """Pin ``config_hash`` + normalize ``proof_mode``, then persist the finalized entry.

    The ``config_hash`` is computed over the canonical serialization of exactly
    ``{agent_id, strategy, model, proof_mode}`` (the normalized proof mode), EXCLUDING the
    incoming ``config_hash`` and ``execution_eligibility`` fields (CON-207).

    Args:
        store: The async repository.
        competition_id: The owning competition.
        entry: The raw agent entry from the API/wire boundary.

    Returns:
        The finalized :class:`~veridex.competition.models.AgentEntry` (with ``config_hash``
        pinned and ``proof_mode`` normalized) that was persisted.
    """
    normalized_proof_mode = normalize_proof_mode(entry.proof_mode)
    config_hash = hashlib.sha256(
        serialize_payload(
            {
                "agent_id": entry.agent_id,
                "strategy": entry.strategy,
                "model": entry.model,
                "proof_mode": normalized_proof_mode,
            }
        ).encode("utf-8")
    ).hexdigest()
    finalized = entry.model_copy(update={"proof_mode": normalized_proof_mode, "config_hash": config_hash})
    await store.add_agent_entry(competition_id, finalized)
    return finalized


async def start_competition(
    store: Store,
    competition_id: str,
    marketstates: list[MarketState],
    agents: list[Agent],
) -> Competition:
    """Run a paper competition and seal its canonical event log (live ≡ projection).

    Flow (codex-corrected ordering):

      1. Load the competition; if already ``FINALIZED`` raise (idempotency gate).
      2. Reject any non-``PAPER`` execution mode (REQ-204A) BEFORE creating any run/events.
      3. Pre-generate ``run_id`` (so the live seq0 + evidence events share the run id
         ``build_event_log`` will project) and advance status to ``RUNNING``.
      4. Persist the seq=0 ``COMPETITION_STARTED`` event.
      5. Run with a stateful live sink that persists one evidence event per sealed ``RunEvent``.
      6. Project the sealed run, VERIFY the live evidence prefix == projection prefix, then
         append ONLY the derived tail (no evidence re-append → no ``UNIQUE(seq)`` collision).
      7. Advance status to ``FINALIZED`` and return the competition with ``run_id`` set.

    Args:
        store: The async repository.
        competition_id: The competition to start.
        marketstates: Ordered tick snapshots driving the run.
        agents: Participating agents (identical inputs per tick).

    Returns:
        The finalized :class:`~veridex.competition.models.Competition` (status ``FINALIZED``,
        ``run_id`` set).

    Raises:
        ValueError: If the competition is already finalized (``competition_already_finalized``),
            the execution mode is not paper (``execution_mode_not_available_in_phase_2a``), or
            the live evidence prefix diverged from the projection
            (``live_evidence_prefix_diverged_from_projection``).
    """
    competition = await store.get_competition(competition_id)

    # 1. idempotency gate — refuse to re-run a finalized competition.
    if competition.status == CompetitionStatus.FINALIZED:
        raise ValueError(_ALREADY_FINALIZED)

    # 2. REQ-204A — only paper trading is available in Phase 2A; create NO run/events otherwise.
    if competition.config.execution_mode != ExecutionMode.PAPER:
        raise ValueError(_EXECUTION_MODE_UNAVAILABLE)

    source_mode = competition.config.source_mode
    agent_ids = [agent.agent_id for agent in agents]
    # Deterministic base ts shared between seq0-build time and finalize (see build_event_log meta).
    base_ts = int(marketstates[0].ts) if marketstates else 0

    # 3. pre-generate the run id (so live events and the projection agree) and go RUNNING.
    run_id = uuid4().hex
    await store.update_competition_status(competition_id, CompetitionStatus.RUNNING)

    # 4. persist seq=0 COMPETITION_STARTED BEFORE the run (byte-identical to build_event_log[0]).
    started = build_competition_started_event(
        competition_id=competition_id,
        run_id=run_id,
        source_mode=source_mode,
        agent_ids=agent_ids,
        base_ts=base_ts,
    )
    await store.append_competition_events(competition_id, [started])

    # 5. run with the stateful live sink (one evidence event per RunEvent, in seq order).
    current_tick_ts = base_ts

    async def sink(run_event: dict[str, Any]) -> None:
        nonlocal current_tick_ts
        event, current_tick_ts = build_evidence_event(
            competition_id=competition_id,
            run_id=run_id,
            run_event=run_event,
            current_tick_ts=current_tick_ts,
        )
        await store.append_competition_events(competition_id, [event])

    run_result = await run_competition(
        marketstates,
        agents,
        source_mode=source_mode,
        run_id=run_id,
        event_sink=sink,
    )

    # 6. finalize — project, verify the live prefix, append ONLY the derived tail.
    meta = {"competition_id": competition_id, "anchor_status": "not_anchored", "event_ts": base_ts}
    full_log = build_event_log(run_result, meta)
    prefix_end = len(run_result.run_events)  # N — last evidence seq
    projection_prefix = [event for event in full_log if event.seq <= prefix_end]  # seq 0..N

    persisted_prefix = await store.list_competition_events(competition_id, since_seq=-1)  # include seq0
    _assert_prefix_parity(persisted_prefix, projection_prefix)

    derived_tail = [event for event in full_log if event.seq > prefix_end]
    await store.append_competition_events(competition_id, derived_tail)

    # 7. finalize the lifecycle; surface run_id on the returned aggregate.
    await store.update_competition_status(competition_id, CompetitionStatus.FINALIZED)
    finalized = await store.get_competition(competition_id)
    return finalized.model_copy(update={"run_id": run_id})


def _assert_prefix_parity(persisted: list[CompetitionEvent], projection: list[CompetitionEvent]) -> None:
    """Raise unless the persisted live prefix is canonically byte-equivalent to the projection.

    Operational timestamps (``persisted_at`` / ``broadcasted_at``) are excluded via
    :meth:`~veridex.competition.events.CompetitionEvent.canonical_dict`.

    Args:
        persisted: The live-persisted evidence prefix (seq 0..N), ascending.
        projection: ``build_event_log``'s prefix (seq 0..N), ascending.

    Raises:
        ValueError: If lengths differ or any canonical field diverges.
    """
    if len(persisted) != len(projection):
        raise ValueError(_PREFIX_MISMATCH)
    for live_event, projected_event in zip(persisted, projection, strict=True):
        if live_event.canonical_dict() != projected_event.canonical_dict():
            raise ValueError(_PREFIX_MISMATCH)
