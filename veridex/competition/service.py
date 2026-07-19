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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from veridex.competition.events import (
    CompetitionEvent,
    build_competition_started_event,
    build_event_log,
    build_evidence_event,
    build_execution_route_event,
)
from veridex.competition.models import (
    AgentEntry,
    Competition,
    CompetitionConfig,
    CompetitionStatus,
    ExecutionMode,
    normalize_proof_mode,
)
from veridex.execution.runner import BreakerCell, _is_real_venue_quote, run_execution_lane
from veridex.policy.circuit_breaker import CircuitBreaker
from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime.evidence import serialize_payload
from veridex.runtime.orchestrator import run_competition
from veridex.venues.sx_bet import FakeVenueAdapter

if TYPE_CHECKING:  # the heavy/offline-safe types are only needed for annotations
    from veridex.deploy.instance import AgentInstance
    from veridex.ingest.marketstate import MarketState
    from veridex.runtime.orchestrator import Agent, RunResult
    from veridex.store import Store
    from veridex.venues.base import VenueAdapter

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


@dataclass(frozen=True)
class LiveExecutionDeps:
    """Operator-supplied dependencies that ARM the live_guarded (real-money) execution path.

    Real money is OPERATOR-ONLY. The operator (not this service) builds an ARMED real-venue
    adapter — a :class:`~veridex.venues.polymarket.PolymarketAdapter` that is
    ``polymarket_write_enabled`` AND ``dry_run=False`` AND has an injected ``write_client`` (its
    ``_require_armed`` triple gate), bound to a :func:`~veridex.venues.polymarket_resolver.resolve_market`
    ``ResolvedMarket`` + side (the T20b-2 resolver: draw→YES on the draw-binary market, O/U on the
    ``-more-markets`` event). The operator also runs
    :func:`~veridex.venues.polymarket_preflight.run_preflight` and passes its
    :attr:`~veridex.venues.polymarket_preflight.PreflightReport.live_ready` verdict here.

    FAIL-CLOSED: when this bundle is absent (the default ``None`` on ``start_competition``) OR
    ``live_ready`` is not ``True`` OR the adapter is not a GENUINE real-venue adapter, the live path
    arms NOTHING and degrades to a dry-run simulation (:func:`_select_execution_route`). No real
    order is ever placed without EVERY gate: ``write_enabled`` AND ``not dry_run`` (inside the
    adapter) AND ``live_ready`` AND a real adapter (here).

    Attributes:
        adapter: The ARMED real-venue adapter (carries ``PROVIDES_REAL_VENUE_QUOTE`` so the lane
            earns ``real_venue_quote``, plus the ``_require_armed`` money gate + resolver-bound
            market/side). Never a Fake / SX-skeleton on the armed path.
        live_ready: The preflight live-readiness gate — ``True`` ONLY when the operator has
            EXPLICITLY verified the neg-risk approval AND the 1-share FAK smoke. The live submit
            arms ONLY when this is ``True`` (fail-closed default ``False``).
    """

    adapter: VenueAdapter
    live_ready: bool = False


# Honest-degrade reason enum (T23) — WHY a configured live_guarded run degraded to a dry-run
# simulation. Checked in fail-closed order; stable strings (telemetry + tests match on these).
_DEGRADE_MISSING_LIVE_DEPS = "missing_live_deps"  # no operator LiveExecutionDeps bundle supplied
_DEGRADE_LIVE_READY_FALSE = "live_ready_false"  # deps present but preflight live-readiness not verified
_DEGRADE_NON_REAL_ADAPTER = "non_real_adapter"  # live_ready but the adapter is not a genuine real venue


def _live_arm_gate(live_deps: LiveExecutionDeps | None) -> str | None:
    """The SINGLE authority for the live_guarded MONEY gates (everything past the mode conjunct).

    Returns ``None`` when EVERY money gate holds — the operator deps are supplied AND ``live_ready``
    is ``True`` AND the adapter is a GENUINE real-venue adapter — i.e. a ``live_guarded`` request may
    ARM. Otherwise returns the SPECIFIC gate-failure reason (checked in fail-closed order), so a
    degrade can describe WHY it fell back to dry. This is meaningful ONLY for a ``live_guarded``
    request; the mode conjunct itself is owned STRUCTURALLY by :func:`_select_execution_route`.

    Both the arm decision (:func:`_select_execution_route`) and the degrade telemetry
    (:func:`_run_execution_block`) read this ONE predicate, so the reason can never disagree with the
    arm outcome (single-authority).
    """
    if live_deps is None:
        return _DEGRADE_MISSING_LIVE_DEPS
    if live_deps.live_ready is not True:
        return _DEGRADE_LIVE_READY_FALSE
    if not _is_real_venue_quote(live_deps.adapter):
        return _DEGRADE_NON_REAL_ADAPTER
    return None


def _select_execution_route(
    execution_mode: ExecutionMode,
    envelope: PolicyEnvelope,
    live_deps: LiveExecutionDeps | None,
) -> tuple[VenueAdapter, str, BreakerCell | None]:
    """Pick ``(adapter, effective_mode_label, guards)`` for the executor lane — FAIL-CLOSED.

    * ``dry_run`` → the deterministic offline :class:`~veridex.venues.sx_bet.FakeVenueAdapter`, the
      ``dry_run`` mode label, and NO breaker guards (paper/dry stay byte-for-byte unaffected).
    * ``live_guarded`` → ARM the operator's real adapter ONLY when EVERY money gate holds:
      ``live_deps`` was supplied AND ``live_deps.live_ready is True`` AND the adapter declares itself
      a GENUINE real-venue adapter (``PROVIDES_REAL_VENUE_QUOTE`` via
      :func:`~veridex.execution.runner._is_real_venue_quote`). When armed, build a FRESH
      :class:`~veridex.execution.runner.BreakerCell` (REQ-2D-404) seeded ``CLOSED`` with the
      envelope's ``cooldown_s``, and run the lane in ``live_guarded`` mode so the runner enforces the
      tighter live stake cap (``live_guarded=True``) and trips the breaker at
      ``envelope.circuit_breaker_threshold`` consecutive live failures. SINGLE-AUTHORITY: this only
      CONSTRUCTS + threads the cell; the runner's policy gate owns every breaker decision.

    Otherwise (no deps / not ``live_ready`` / not a real adapter) FAIL CLOSED: degrade to a dry-run
    simulation (Fake + ``dry_run`` label + no guards) so NO real order path is ever reached — the run
    stays dry and finalizes honestly rather than touching real money.

    Args:
        execution_mode: The competition's configured mode (never ``paper`` — the caller gates that).
        envelope: The policy envelope (supplies ``cooldown_s`` for the breaker; the runner reads
            ``circuit_breaker_threshold`` / ``max_stake_live_guarded`` directly).
        live_deps: The operator's armed live dependencies, or ``None`` (fail-closed default).

    Returns:
        ``(adapter, effective_mode, guards)`` — ``effective_mode`` is the string the runner receives
        (may be degraded to ``"dry_run"``); ``guards`` is a ``BreakerCell`` ONLY on the armed live
        path, else ``None``.
    """
    if execution_mode == ExecutionMode.DRY_RUN:
        return FakeVenueAdapter(), ExecutionMode.DRY_RUN.value, None

    # ARM the real-money path ONLY when the mode is EXPLICITLY live_guarded AND every real-money gate
    # holds. STRUCTURAL fail-closed (defense-in-depth): making ``execution_mode == LIVE_GUARDED`` the
    # FIRST conjunct means ANY other mode — a defensively-passed ``paper`` or a future 4th ExecutionMode
    # value — degrades to dry HERE, rather than relying on incidental enum arithmetic (that ``dry_run``
    # is caught above and ``paper`` is gated at a distant caller). No mode outside live_guarded can ever
    # reach a real submit, even with FULL armed operator deps.
    if execution_mode == ExecutionMode.LIVE_GUARDED and _live_arm_gate(live_deps) is None:
        # STRUCTURAL fail-closed: ``execution_mode == LIVE_GUARDED`` stays the FIRST conjunct (no
        # other mode can arm), and the three MONEY gates are the single-authority ``_live_arm_gate``
        # (``None`` ⇒ deps present AND live_ready AND a genuine real adapter). The same predicate
        # backs the degrade telemetry, so arm-outcome and degrade-reason can never disagree.
        assert live_deps is not None  # narrowed by the gate returning None; defense-in-depth
        guards = BreakerCell(CircuitBreaker(), cooldown_s=float(envelope.cooldown_s))
        return live_deps.adapter, ExecutionMode.LIVE_GUARDED.value, guards

    # FAIL-CLOSED: not an armed live_guarded run → degrade to a dry-run simulation (no real order).
    return FakeVenueAdapter(), ExecutionMode.DRY_RUN.value, None


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


class RosterInstanceNotFoundError(ValueError):
    """Raised when a roster entry references a deployed instance the caller may not bind, in a way that
    must NOT leak the instance's existence: it is absent, OR unowned-legacy (``operator_id is None``).

    Mirrors I-2's instance owner-scoping (``deploy.py`` ``GET /agents/instances/{id}``): both cases are
    INDISTINGUISHABLE to a non-owner and map to HTTP 404 with no ``config_hash`` disclosure.
    """


class RosterInstanceNotOwnedError(ValueError):
    """Raised when a roster entry references a deployed instance owned by a DIFFERENT principal.

    Mirrors I-2's instance owner-scoping: maps to HTTP 403 (owned-by-another) — the check runs BEFORE
    any ``config_hash`` is read, so a non-owner never learns the deployed identity fingerprint.
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


async def _resolve_owned_instance(
    store: Store, instance_id: str, principal_did: str | None
) -> AgentInstance:
    """Resolve a deployed instance the caller OWNS, mirroring I-2's owner-scoping (fail-closed, no leak).

    Mirrors :func:`veridex.api.deploy.get_agent_instance` (``GET /agents/instances/{id}``): absent OR
    unowned-legacy (``operator_id is None``) → :class:`RosterInstanceNotFoundError` (404, no existence
    leak); owned-by-another → :class:`RosterInstanceNotOwnedError` (403). The ownership decision is made
    BEFORE the caller reads the instance's ``config_hash``, so a non-owner never learns the deployed
    identity fingerprint.

    Args:
        store: The async repository.
        instance_id: The deployed instance the roster entry references.
        principal_did: The authenticated caller's DID; ``None`` (unauthenticated) is refused as
            not-found (fail-closed — never resolve without a caller).

    Returns:
        The caller's own :class:`~veridex.deploy.instance.AgentInstance`.

    Raises:
        RosterInstanceNotFoundError: Absent, unowned-legacy, or unauthenticated (404, no leak).
        RosterInstanceNotOwnedError: Owned by a different principal (403).
    """
    if principal_did is None:
        raise RosterInstanceNotFoundError("agent instance not found")
    try:
        instance = await store.get_agent_instance(instance_id)
    except KeyError as exc:
        raise RosterInstanceNotFoundError("agent instance not found") from exc
    # An unowned / legacy row is never inherited — hidden as if it does not exist (mirror I-2).
    if instance.operator_id is None:
        raise RosterInstanceNotFoundError("agent instance not found")
    if instance.operator_id != principal_did:
        raise RosterInstanceNotOwnedError("principal does not own this agent instance")
    return instance


#: The lifecycle statuses in which a competition's roster may still be mutated (I-7b). Once the arena
#: (or start) claims the competition out of this window, ``add_agent_entry_guarded`` refuses any append,
#: so a registration racing a start can never mutate a roster the run has already frozen (Codex M1).
_ROSTER_MUTABLE_STATUSES: tuple[CompetitionStatus, ...] = (CompetitionStatus.DRAFT, CompetitionStatus.OPEN)


def declared_config_hash(*, agent_id: str, strategy: str, model: str | None, proof_mode: str) -> str:
    """Content hash of a DECLARED entry's identity (CON-207): ``{agent_id, strategy, model, proof_mode}``.

    The single canonical serializer for a declared roster entry's identity commitment. ``proof_mode`` MUST
    already be the normalized canonical value (``"reproducible"`` / ``"verified"``). The intrinsic arena's
    strict config contract (Codex M2) recomputes an EXPECTED hash with this same helper and compares it to
    each registered entry's pinned ``config_hash`` — so equal ids/strategy labels alone can never admit a
    roster whose pinned model/proof identity differs from the contestant the arena actually runs.

    Args:
        agent_id: The declared agent identifier.
        strategy: The declared strategy label.
        model: The declared model slug (or ``None``).
        proof_mode: The already-normalized proof mode.

    Returns:
        The hex SHA-256 identity hash.
    """
    return hashlib.sha256(
        serialize_payload(
            {
                "agent_id": agent_id,
                "strategy": strategy,
                "model": model,
                "proof_mode": proof_mode,
            }
        ).encode("utf-8")
    ).hexdigest()


async def register_agent(
    store: Store,
    competition_id: str,
    entry: AgentEntry,
    *,
    principal_did: str | None = None,
) -> AgentEntry:
    """Pin ``config_hash`` + normalize ``proof_mode``, then persist the finalized entry.

    Two roster kinds (I-7):

    - **Instance-bound** (``entry.instance_id`` set): the entry references a Studio-deployed
      :class:`~veridex.deploy.instance.AgentInstance`. The referenced instance is OWNER-SCOPED exactly
      as I-2 scopes it (``deploy.py`` ``GET /agents/instances/{id}``): the resolve + ownership check runs
      BEFORE any ``config_hash`` is read, and only the caller's OWN instance binds. A non-owned / legacy
      unowned / absent instance is refused with NO ``config_hash`` disclosure (a non-owner may not even
      learn the deployed identity fingerprint). On success we PIN that instance's identity — its
      ``instance_id`` + its **deployment** ``config_hash`` — onto the entry, so the arena runs the ACTUAL
      deployed contestant at start (:func:`~veridex.api.demo_fixtures.bind_roster_instance`). The label
      hash is deliberately NOT recomputed here: the deployment ``config_hash`` (a ``DeployConfig``
      serialization) and the label hash below are DIFFERENT serializers, so recomputing would clobber the
      pinned deployment hash and make the start-time binding FALSE-drift (HTTP 400) for every bound entry.
    - **Declared** (no ``instance_id``): unchanged legacy behavior — the ``config_hash`` is computed
      over the canonical serialization of exactly ``{agent_id, strategy, model, proof_mode}`` (the
      normalized proof mode), EXCLUDING the incoming ``config_hash`` and ``execution_eligibility``
      fields (CON-207). ``principal_did`` is unused for a declared entry.

    Args:
        store: The async repository.
        competition_id: The owning competition.
        entry: The raw agent entry from the API/wire boundary.
        principal_did: The authenticated caller's Privy DID (threaded from the register endpoint's
            ``require_principal``). REQUIRED to bind an instance-bound entry — the ownership check is
            fail-closed, so a missing principal on a bound entry is refused as not-found (no leak).

    Returns:
        The finalized :class:`~veridex.competition.models.AgentEntry` (with ``config_hash``
        pinned and ``proof_mode`` normalized) that was persisted.

    Raises:
        RosterInstanceNotFoundError: If a bound entry's instance is absent or unowned-legacy, or the
            caller is unauthenticated (mirrors I-2 → HTTP 404, no existence leak).
        RosterInstanceNotOwnedError: If a bound entry's instance is owned by another principal
            (mirrors I-2 → HTTP 403).
    """
    normalized_proof_mode = normalize_proof_mode(entry.proof_mode)
    if entry.instance_id is not None:
        instance = await _resolve_owned_instance(store, entry.instance_id, principal_did)
        finalized = entry.model_copy(
            update={
                "proof_mode": normalized_proof_mode,
                "config_hash": instance.config_hash,
                "instance_id": instance.instance_id,
            }
        )
    else:
        config_hash = declared_config_hash(
            agent_id=entry.agent_id,
            strategy=entry.strategy,
            model=entry.model,
            proof_mode=normalized_proof_mode,
        )
        finalized = entry.model_copy(update={"proof_mode": normalized_proof_mode, "config_hash": config_hash})
    # Atomic status-guarded admission (Codex M1): append ONLY IF the roster is still mutable, with the
    # duplicate-id + capacity checks in the SAME write, so a registration racing an arena/start claim can
    # never append after the run has frozen the roster. Raises RosterAdmissionError on any guard failure.
    await store.add_agent_entry_guarded(
        competition_id, finalized, mutable_statuses=_ROSTER_MUTABLE_STATUSES
    )
    return finalized


async def start_competition(
    store: Store,
    competition_id: str,
    marketstates: list[MarketState],
    agents: list[Agent],
    *,
    broadcast: BroadcastFn | None = None,
    live_deps: LiveExecutionDeps | None = None,
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
        live_deps: Operator-supplied :class:`LiveExecutionDeps` that ARM the live_guarded path
            (real adapter + ``live_ready``). ``None`` (the default) FAILS CLOSED: a ``live_guarded``
            run degrades to a dry-run simulation and no real order is ever placed (REQ-2D-701).

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
                live_deps=live_deps,
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
    live_deps: LiveExecutionDeps | None = None,
) -> None:
    """Run the policy-gated executor lane and persist+broadcast its derived events.

    Sources the :class:`~veridex.policy.envelope.PolicyEnvelope` from
    ``competition.config.policy_envelope`` (or a conservative default), pins its ``policy_hash``
    (REQ-2B-03 — recorded on every emitted ``POLICY_RESULT`` payload), then routes the venue adapter
    + effective mode + breaker guards via :func:`_select_execution_route` (FAIL-CLOSED):

    * ``dry_run`` → the deterministic Fake, no guards.
    * ``live_guarded`` ARMED (``live_deps`` present, ``live_ready`` True, real adapter) → the
      operator's real :class:`~veridex.venues.polymarket.PolymarketAdapter` (resolver-bound market/
      side, ``_require_armed`` money gate) + a :class:`~veridex.execution.runner.BreakerCell`
      threaded as ``guards`` so the runner enforces the live stake cap + circuit breaker (REQ-2D-404).
    * ``live_guarded`` NOT armed → degrades to a dry-run simulation (no real order).

    The returned events are appended to the canonical log as ONE contiguous block
    (persist-before-broadcast), then each is broadcast; a broadcast error never aborts the run.

    Args:
        store: The async repository.
        competition: The competition (provides the roster + config envelope).
        run_result: The sealed, frozen Phase-1 run (read-only).
        execution_mode: ``dry_run`` or ``live_guarded`` (never ``paper`` — caller-gated).
        base_seq: First competition ``seq`` for the execution block (strictly after the 2A tail).
        event_ts: Deterministic event timestamp (the meta base ts).
        broadcast: Optional persist-before-broadcast callback.
        live_deps: Operator-supplied armed live dependencies, or ``None`` (fail-closed default).
    """
    envelope = competition.config.policy_envelope or _default_policy_envelope()
    # REQ-2B-03: pin the policy commitment. It is recorded on every POLICY_RESULT event the lane
    # emits (result.policy_hash == envelope.policy_hash()), binding the persisted execution block
    # to this exact envelope alongside the per-agent config_hash already in the evidence log.
    _ = envelope.policy_hash()

    # FAIL-CLOSED routing: pick the adapter, the effective mode label the runner receives (a
    # not-fully-armed live_guarded run degrades to "dry_run"), and the breaker guards (armed live
    # ONLY). service.py CONSTRUCTS the BreakerCell; the runner's policy gate owns every decision.
    adapter, effective_mode, guards = _select_execution_route(execution_mode, envelope, live_deps)
    entries_by_agent = {entry.agent_id: entry for entry in competition.entries}

    # Honest-degrade telemetry (T23): a CONFIGURED live_guarded run that FAILED a money gate was
    # routed to a dry-run simulation above (fail-closed — no real order). Record WHY as an
    # EXECUTION_ROUTE event: evidence=False OPS telemetry an auditor reads in the log, so the degrade
    # SELF-DESCRIBES rather than being inferred from config=live vs events=dry. The reason comes from
    # the SAME _live_arm_gate the router armed on (single-authority). Sealed evidence is untouched —
    # this derived event lands strictly after the 2A tail (base_seq), so the lane block follows it.
    lane_seq = base_seq
    if execution_mode == ExecutionMode.LIVE_GUARDED and effective_mode != ExecutionMode.LIVE_GUARDED.value:
        degrade_event = build_execution_route_event(
            competition_id=competition.competition_id,
            run_id=run_result.run_id,
            seq=lane_seq,
            event_ts=event_ts,
            payload={
                "requested_execution_mode": ExecutionMode.LIVE_GUARDED.value,
                "effective_execution_mode": effective_mode,
                "degraded_because_not_armed": True,
                "degrade_reason": _live_arm_gate(live_deps),
            },
        )
        # Persist-before-broadcast, exactly like the lane block below.
        await store.append_competition_events(competition.competition_id, [degrade_event])
        if broadcast is not None:
            await _safe_broadcast(broadcast, degrade_event)
        lane_seq += 1

    events = await run_execution_lane(
        store,
        competition_id=competition.competition_id,
        run_result=run_result,
        envelope=envelope,
        adapter=adapter,
        entries_by_agent=entries_by_agent,
        execution_mode=effective_mode,
        base_seq=lane_seq,
        event_ts=event_ts,
        guards=guards,
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
