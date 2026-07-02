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
from collections.abc import Awaitable, Callable
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
from veridex.execution.runner import run_execution_lane
from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime.evidence import serialize_payload
from veridex.runtime.orchestrator import run_competition
from veridex.venues.sx_bet import FakeVenueAdapter, SXBetAdapter

if TYPE_CHECKING:  # the heavy/offline-safe types are only needed for annotations
    from veridex.ingest.marketstate import MarketState
    from veridex.runtime.orchestrator import Agent, RunResult
    from veridex.store import Store

# A live-broadcast callback: persist-before-broadcast is the caller's responsibility, so this is
# invoked only AFTER the event is durably appended. Errors are swallowed by the service so a dead
# spectator never aborts the run (REQ-2B-30).
BroadcastFn = Callable[[CompetitionEvent], Awaitable[None]]


def _default_policy_envelope() -> PolicyEnvelope:
    """Build the conservative, deny-by-default envelope used when none is configured.

    Fail-closed posture: empty venue/market allowlists deny every action, and a zero
    human-approval threshold escalates any otherwise-clean action. An operator that wants
    real fills MUST commit an explicit ``policy_envelope`` on the competition config.

    Returns:
        A restrictive :class:`~veridex.policy.envelope.PolicyEnvelope`.
    """
    return PolicyEnvelope(
        max_stake=10.0,
        max_orders_per_run=5,
        max_orders_per_session=5,
        max_orders_per_day=5,
        venue_allowlist=[],
        market_allowlist=[],
        min_edge_bps=0,
        max_slippage_bps=0,
        max_price=1_000.0,
        max_quote_age_s=300,
        cooldown_s=0,
        human_approval_threshold=0.0,
        kill_switch=False,
    )


# Stable reason strings (tests match on these EXACTLY — do not edit casually).
_ALREADY_FINALIZED = "competition_already_finalized"
_ALREADY_RUNNING = "competition_already_running"
_PREFIX_MISMATCH = "live_evidence_prefix_diverged_from_projection"


# ---------------------------------------------------------------------------
# Typed exception hierarchy (all subclass ValueError so existing match tests pass)
# ---------------------------------------------------------------------------


class CompetitionConflictError(ValueError):
    """Raised when an operation is rejected due to the competition's current lifecycle state.

    Covers idempotency gates (already finalized / already running).  Maps to HTTP 409.
    """


class CompetitionStateError(ValueError):
    """Raised when an operation is unavailable in the current Phase (e.g. non-paper modes).

    Maps to HTTP 400 — the request is understood but not executable under Phase-2A rules.
    """


class CompetitionIntegrityError(ValueError):
    """Raised when the live evidence prefix diverges from the deterministic projection.

    Indicates an internal consistency violation (CON-203 trust invariant breach).
    Maps to HTTP 500.
    """


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
    *,
    broadcast: BroadcastFn | None = None,
) -> Competition:
    """Run a competition, seal its canonical log, then (non-paper) run the executor lane.

    Flow (codex-corrected ordering):

      1. Load the competition; if already ``FINALIZED``/``RUNNING`` raise (idempotency gate).
      2. Pre-generate ``run_id`` (so the live seq0 + evidence events share the run id
         ``build_event_log`` will project) and advance status to ``RUNNING``.
      3. Persist the seq=0 ``COMPETITION_STARTED`` event.
      4. Run with ``store=store`` (persists the sealed ``RunResult`` for external verification)
         and a stateful live sink that persists one evidence event per sealed ``RunEvent``.
      5. Project the sealed run, VERIFY the live evidence prefix == projection prefix (AC-213,
         over the sealed seq 0..N ONLY), then append ONLY the 2A derived tail.
      6. EXECUTION (non-paper only, DOWNSTREAM of the seal): run the policy-gated executor lane
         and append its events as a SECOND derived block (seq strictly after the 2A tail) —
         EXCLUDED from the AC-213 prefix check. ``paper`` keeps the exact no-execution behavior.
      7. Advance status to ``FINALIZED`` and return the competition with ``run_id`` set.

    Args:
        store: The async repository.
        competition_id: The competition to start.
        marketstates: Ordered tick snapshots driving the run.
        agents: Participating agents (identical inputs per tick).
        broadcast: Optional persist-before-broadcast callback. Each evidence event (live sink)
            and each execution event is appended to the store FIRST, then broadcast; broadcast
            errors are swallowed and never abort the run (REQ-2B-17/30, REQ-2D-105).

    Returns:
        The finalized :class:`~veridex.competition.models.Competition` (status ``FINALIZED``,
        ``run_id`` set).

    Raises:
        CompetitionConflictError: If the competition is already finalized
            (``competition_already_finalized``) or already running
            (``competition_already_running``).
        CompetitionIntegrityError: If the live evidence prefix diverged from the projection
            (``live_evidence_prefix_diverged_from_projection…``).  Both subclass ``ValueError``
            so existing ``match`` tests remain valid.
        NotImplementedError: If ``live_guarded`` is requested while the live venue adapter is
            not enabled (the expected testnet-gated behavior).
    """
    competition = await store.get_competition(competition_id)

    # 1. idempotency gate — refuse to re-run a finalized competition.
    if competition.status == CompetitionStatus.FINALIZED:
        raise CompetitionConflictError(_ALREADY_FINALIZED)

    # A2. Reject RUNNING before any mutation to prevent seq0 re-append / UNIQUE(seq) collision.
    if competition.status == CompetitionStatus.RUNNING:
        raise CompetitionConflictError(_ALREADY_RUNNING)

    execution_mode = competition.config.execution_mode
    source_mode = competition.config.source_mode
    agent_ids = [agent.agent_id for agent in agents]
    # Deterministic base ts shared between seq0-build time and finalize (see build_event_log meta).
    base_ts = int(marketstates[0].ts) if marketstates else 0

    # 3. pre-generate the run id (so live events and the projection agree) and go RUNNING.
    run_id = uuid4().hex
    await store.update_competition_status(competition_id, CompetitionStatus.RUNNING)
    # A1. Persist run_id immediately after RUNNING so store.get_competition().run_id is set.
    await store.update_competition_run_id(competition_id, run_id)

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
        # Persist-before-broadcast (DEC-2D-4): the store append MUST complete before the
        # broadcast is attempted, so a spectator can never see an event that isn't durably
        # persisted. Broadcasting only when a callback was provided keeps the no-broadcast
        # path (existing callers) byte-identical to today.
        await store.append_competition_events(competition_id, [event])
        if broadcast is not None:
            await _safe_broadcast(broadcast, event)

    # ``store=store`` persists the SEALED RunResult (runs/run_events/score_rows) under the same
    # pre-generated run_id the competition references — so a verifier can later load_run(run_id)
    # and recompute the evidence hash to confirm each evidence CompetitionEvent binds to it
    # (REQ-208/AC-203). The live sink writes the separate competition_events table; there is no
    # double-persist or UNIQUE collision between the two paths.
    run_result = await run_competition(
        marketstates,
        agents,
        source_mode=source_mode,
        run_id=run_id,
        store=store,
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

    # 6b. EXECUTION (non-paper only) — DOWNSTREAM of the seal + the 2A finalize. Appended as a
    # SECOND derived block whose seq starts strictly after the 2A tail, so it is EXCLUDED from
    # the AC-213 prefix-parity check above (which already ran over seq 0..N). ``paper`` skips this
    # entirely, preserving the exact existing no-execution behavior.
    if execution_mode != ExecutionMode.PAPER:
        next_seq = max((event.seq for event in full_log), default=0) + 1
        try:
            await _run_execution_block(
                store,
                competition=competition,
                run_result=run_result,
                execution_mode=execution_mode,
                base_seq=next_seq,
                event_ts=base_ts,
                broadcast=broadcast,
            )
        except Exception:
            # The seal + the entire 2A tail (incl. COMPETITION_FINALIZED) are already durable.
            # The executor lane is NON-SCORING and downstream-of-seal — the same best-effort tier
            # as the spectator broadcast — so a venue failure (e.g. the testnet-gated live adapter
            # raising NotImplementedError) must NOT strand the lifecycle in RUNNING. Finalize the
            # status first, then re-raise so the router still surfaces the error (e.g. 501).
            await store.update_competition_status(competition_id, CompetitionStatus.FINALIZED)
            raise

    # 7. finalize the lifecycle; run_id is already in the store (A1), so a plain load suffices.
    await store.update_competition_status(competition_id, CompetitionStatus.FINALIZED)
    return await store.get_competition(competition_id)


async def _run_execution_block(
    store: Store,
    *,
    competition: Competition,
    run_result: RunResult,
    execution_mode: ExecutionMode,
    base_seq: int,
    event_ts: int,
    broadcast: BroadcastFn | None,
) -> None:
    """Run the policy-gated executor lane and persist+broadcast its derived events.

    Sources the :class:`~veridex.policy.envelope.PolicyEnvelope` from
    ``competition.config.policy_envelope`` (or a conservative default), pins its ``policy_hash``
    (REQ-2B-03 — recorded on every emitted ``POLICY_RESULT`` payload), and picks the venue
    adapter by mode (``dry_run`` → fake, ``live_guarded`` → SX Bet skeleton). The returned events
    are appended to the canonical log as ONE contiguous block (persist-before-broadcast), then
    each is broadcast; a broadcast error never aborts the run.

    Args:
        store: The async repository.
        competition: The competition (provides the roster + config envelope).
        run_result: The sealed, frozen Phase-1 run (read-only).
        execution_mode: ``dry_run`` or ``live_guarded`` (never ``paper`` — caller-gated).
        base_seq: First competition ``seq`` for the execution block (strictly after the 2A tail).
        event_ts: Deterministic event timestamp (the meta base ts).
        broadcast: Optional persist-before-broadcast callback.
    """
    envelope = competition.config.policy_envelope or _default_policy_envelope()
    # REQ-2B-03: pin the policy commitment. It is recorded on every POLICY_RESULT event the lane
    # emits (result.policy_hash == envelope.policy_hash()), binding the persisted execution block
    # to this exact envelope alongside the per-agent config_hash already in the evidence log.
    _ = envelope.policy_hash()

    adapter = FakeVenueAdapter() if execution_mode == ExecutionMode.DRY_RUN else SXBetAdapter()
    entries_by_agent = {entry.agent_id: entry for entry in competition.entries}

    events = await run_execution_lane(
        store,
        competition_id=competition.competition_id,
        run_result=run_result,
        envelope=envelope,
        adapter=adapter,
        entries_by_agent=entries_by_agent,
        execution_mode=execution_mode.value,
        base_seq=base_seq,
        event_ts=event_ts,
    )

    # Persist the whole block FIRST (persist-before-broadcast), then fan out.
    await store.append_competition_events(competition.competition_id, events)
    if broadcast is not None:
        for event in events:
            await _safe_broadcast(broadcast, event)


async def _safe_broadcast(broadcast: BroadcastFn, event: CompetitionEvent) -> None:
    """Invoke ``broadcast`` swallowing any error — a dead spectator never aborts the run."""
    try:
        await broadcast(event)
    except Exception:  # noqa: BLE001 — live fanout is best-effort; the run must not fail on it.
        return


def _assert_prefix_parity(persisted: list[CompetitionEvent], projection: list[CompetitionEvent]) -> None:
    """Raise unless the persisted live prefix is canonically byte-equivalent to the projection.

    Operational timestamps (``persisted_at`` / ``broadcasted_at``) are excluded via
    :meth:`~veridex.competition.events.CompetitionEvent.canonical_dict`.

    The error message embeds the first divergent ``seq`` value (A3 diagnostic) while keeping
    the stable ``live_evidence_prefix_diverged_from_projection`` prefix so existing ``match``
    patterns still work.

    Args:
        persisted: The live-persisted evidence prefix (seq 0..N), ascending.
        projection: ``build_event_log``'s prefix (seq 0..N), ascending.

    Raises:
        CompetitionIntegrityError: If lengths differ or any canonical field diverges.  The
            message has the form ``"{_PREFIX_MISMATCH}: seq={s} ({detail})"`` where ``s``
            is the first divergent sequence number.  Subclasses ``ValueError`` so existing
            ``pytest.raises(ValueError, match=...)`` assertions remain valid.
    """
    if len(persisted) != len(projection):
        # The first "missing" seq is whichever side is shorter.
        s = min(len(persisted), len(projection))
        raise CompetitionIntegrityError(
            f"{_PREFIX_MISMATCH}: seq={s} (length mismatch: persisted={len(persisted)} projection={len(projection)})"
        )
    for live_event, projected_event in zip(persisted, projection, strict=True):
        if live_event.canonical_dict() != projected_event.canonical_dict():
            raise CompetitionIntegrityError(f"{_PREFIX_MISMATCH}: seq={live_event.seq} (field mismatch)")
