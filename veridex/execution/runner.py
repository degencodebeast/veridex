"""Executor lane — Phase 2B Task 6 (the receipt≠skill keystone shell).

This async shell turns the SEALED competition result into execution attempts, gated by the
deny-by-default policy engine. It is the load-bearing trust boundary: a venue fill is
*production-readiness* evidence, NEVER *skill* evidence. Concretely:

  * The lane consumes :func:`veridex.strategies.value.value_proposals`, which reads ONLY the
    sealed ``score_rows`` (deterministic-law ``recomputed_edge_bps`` / ``valid``). It NEVER
    reads the LLM-claimed edge, and it NEVER calls ``run_competition`` or mutates the
    ``RunResult``. Running the lane therefore leaves ``score_run`` / ``leaderboard`` /
    ``evidence_hash`` byte-identical to NOT running it (AC-2B-05).
  * Every emitted :class:`~veridex.competition.events.CompetitionEvent` is DERIVED
    (``evidence=False`` with non-empty ``derived_from``) via the Task-4 builders.
  * Persistence here is limited to NON-SCORING :class:`~veridex.execution.models.ExecutionRecord`
    rows. The caller (Task 7) owns appending/broadcasting the returned events to the canonical
    competition log; this function only returns them (and optionally mirrors each to a live
    ``broadcast`` hook).

CON-010 (async shell / sync core): I/O (venue quote/submit) is async here; the policy engine,
strategy selection, and receipt normalization stay sync and are CALLED from this shell.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from veridex.competition.events import (
    CompetitionEvent,
    build_approval_audit_event,
    build_execution_receipt_event,
    build_execution_submitted_event,
    build_policy_result_event,
)
from veridex.execution.legibility import mispricing_gap_bps
from veridex.execution.models import ExecutionRecord, ExecutionStatus
from veridex.law.edge import executable_edge_bps
from veridex.policy.circuit_breaker import CircuitBreaker
from veridex.policy.engine import PolicyDecision
from veridex.policy.gate import (
    PostQuoteContext,
    PreQuoteContext,
    evaluate_post_quote,
    evaluate_pre_quote,
)
from veridex.strategies.value import value_proposals
from veridex.venues.base import Order, OrderStatus, poll_order_terminal

if TYPE_CHECKING:
    from veridex.competition.models import AgentEntry
    from veridex.policy.envelope import PolicyEnvelope
    from veridex.runtime.orchestrator import RunResult
    from veridex.store import Store
    from veridex.venues.base import VenueAdapter

# Execution-mode labels (mirror veridex.competition.models.ExecutionMode values).
_PAPER = "paper"
_DRY_RUN = "dry_run"
_LIVE_GUARDED = "live_guarded"

# Receipt terminals that count as an EXECUTED FAILURE for the circuit breaker (REQ-2D-404). A
# rejected / expired / cancelled / honestly-UNRESOLVED live fill is a real venue failure; only a
# FILLED / PARTIAL is a success. NOTE: this moves the breaker ONLY for a REAL executed (live_guarded)
# outcome — a policy DENY never reaches a receipt, so a denial can never trip the breaker.
_EXECUTED_FAILURE_TERMINALS: frozenset[ExecutionStatus] = frozenset(
    {
        ExecutionStatus.REJECTED,
        ExecutionStatus.EXPIRED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.UNRESOLVED,
    }
)

# The adapter attribute a GENUINE real-venue adapter (e.g. PolymarketAdapter) sets True to declare
# that its ``quote_market`` is a real venue quote. FAIL-CLOSED: absent/False for Fake/paper/dry
# adapters, so ``real_venue_quote`` defaults False and is EARNED only by an explicit real adapter —
# NEVER inferred from the presence of a venue price / edge in the payload.
_REAL_VENUE_QUOTE_MARKER = "PROVIDES_REAL_VENUE_QUOTE"


class BreakerCell:
    """Mutable carrier threading the live circuit-breaker state across a single lane run.

    ``run_execution_lane`` (and :func:`resolve_approval`) read the breaker into EACH proposal's
    pre-quote context and, on the LIVE-guarded path ONLY, update it around EXECUTED outcomes
    (success/failure). Threading it through one mutable cell means a mid-lane trip blocks the
    remaining proposals (fail-closed) and the caller can observe the final state. Passing ``None``
    (the default) makes the lane use a transparent, always-``CLOSED`` breaker so paper/dry-run — and
    any caller that does not opt in — are byte-for-byte unaffected.

    Attributes:
        breaker: The current (immutable) :class:`CircuitBreaker` state; reassigned as it transitions.
        cooldown_s: Seconds the breaker must stay ``OPEN`` before the lane admits a recovery probe
            (applied once, via :meth:`CircuitBreaker.resolve`, at the start of the run).
    """

    def __init__(self, breaker: CircuitBreaker | None = None, *, cooldown_s: float = 0.0) -> None:
        self.breaker = breaker if breaker is not None else CircuitBreaker()
        self.cooldown_s = cooldown_s


def _is_real_venue_quote(adapter: VenueAdapter) -> bool:
    """Return whether ``adapter`` declares its quotes as GENUINE real-venue quotes (fail-closed).

    Reads the explicit :data:`_REAL_VENUE_QUOTE_MARKER` class flag a real adapter sets; a missing
    or falsey marker (Fake / SX-skeleton / any paper-dry adapter) yields ``False``. This is the ONLY
    input to ``real_venue_quote`` — the flag is never inferred from the quote's numbers.
    """
    return bool(getattr(adapter, _REAL_VENUE_QUOTE_MARKER, False))

# Stake sizing. The order is sized from the SEALED law ``kelly_fraction`` (never re-derived from
# a venue price) using a conservative half-Kelly fraction against a fixed deterministic bankroll,
# capped at the policy ``max_stake``. When the law advised no sizing (kelly <= 0) a small
# deterministic fallback stake is used so a law-approved take still places a token order.
_FRACTIONAL_KELLY_MULTIPLIER = 0.5  # half-Kelly: full Kelly over-bets / carries tail risk
_DEFAULT_BANKROLL = 1000.0
_FALLBACK_STAKE = 1.0

# Canonical post-``policy_approved`` advance path keyed by the receipt's terminal status, so a
# record walks the legal lifecycle (proposed→law_approved→policy_approved→submitted→…) without
# illegal skips. Unknown terminals fall back to a bare ``submitted`` step.
_POST_POLICY_PATH: dict[ExecutionStatus, tuple[ExecutionStatus, ...]] = {
    ExecutionStatus.FILLED: (ExecutionStatus.SUBMITTED, ExecutionStatus.ACCEPTED, ExecutionStatus.FILLED),
    ExecutionStatus.PARTIAL: (ExecutionStatus.SUBMITTED, ExecutionStatus.ACCEPTED, ExecutionStatus.PARTIAL),
    ExecutionStatus.ACCEPTED: (ExecutionStatus.SUBMITTED, ExecutionStatus.ACCEPTED),
    ExecutionStatus.REJECTED: (ExecutionStatus.SUBMITTED, ExecutionStatus.REJECTED),
    ExecutionStatus.EXPIRED: (ExecutionStatus.SUBMITTED, ExecutionStatus.EXPIRED),
}


def _venue_name(adapter: VenueAdapter) -> str:
    """Resolve the venue slug for an adapter.

    Prefers an explicit ``venue`` attribute; otherwise derives a slug from the class name
    (e.g. ``FakeVenueAdapter`` -> ``"fake"``). The slug is the value the policy engine checks
    against ``venue_allowlist`` and that is stamped onto the order/receipt.

    Args:
        adapter: The venue adapter in use.

    Returns:
        A lowercase venue slug.
    """
    explicit = getattr(adapter, "venue", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    name = type(adapter).__name__
    for suffix in ("VenueAdapter", "Adapter"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.lower() or name


def _advance_through(record: ExecutionRecord, path: tuple[ExecutionStatus, ...]) -> None:
    """Advance ``record`` through each status in ``path`` (enforcing the lifecycle table)."""
    for status in path:
        record.advance(status)


def _size_stake(kelly_fraction: float, *, bankroll: float, max_stake: float) -> float:
    """Size a per-order stake from the SEALED law ``kelly_fraction``, capped at ``max_stake``.

    Half-Kelly against a fixed bankroll: ``0.5 * kelly_fraction * bankroll``, capped at the
    policy ``max_stake``. A non-positive or ``NaN`` Kelly fraction (the law advised no sizing, or
    an undefined value) falls back to a small deterministic stake (also capped). This reads ONLY
    the sealed law output — the lane never re-derives Kelly/edge from a venue price (the law owns
    that math).

    Args:
        kelly_fraction: The sealed advisory Kelly fraction from the score row.
        bankroll: Fixed deterministic bankroll the fraction is applied against.
        max_stake: Policy hard cap on a single order's stake.

    Returns:
        The sized stake, never exceeding ``max_stake``.
    """
    # NaN guard: ``NaN <= 0.0`` is False, so without this an undefined Kelly would bypass the
    # fallback and yield a NaN order size.
    if kelly_fraction <= 0.0 or math.isnan(kelly_fraction):
        return min(_FALLBACK_STAKE, max_stake)
    return min(_FRACTIONAL_KELLY_MULTIPLIER * kelly_fraction * bankroll, max_stake)


def _slippage_bps(reference_price: float, quote_price: float) -> int:
    """Absolute deviation (bps) of the quote from the sealed reference price; 0 if no reference.

    The sealed ``reference_price`` is the entry decimal price the deterministic law committed
    (``Proposal.reference_price``); the post-quote gate measures how far the ACTUAL venue quote has
    drifted from it. A non-positive reference (absent in the sealed evidence) yields ``0`` — a
    missing reference cannot manufacture slippage, and the edge/staleness rules still gate.

    Args:
        reference_price: The sealed entry decimal price for ``(market_key, side)``.
        quote_price: The decimal price actually quoted by the venue.

    Returns:
        ``round(|quote - reference| / reference * 10000)``; ``0`` when ``reference_price <= 0``.
    """
    if reference_price <= 0.0:
        return 0
    return round(abs(quote_price - reference_price) / reference_price * 10000)


async def run_execution_lane(
    store: Store,
    *,
    competition_id: str,
    run_result: RunResult,
    envelope: PolicyEnvelope,
    adapter: VenueAdapter,
    entries_by_agent: dict[str, AgentEntry],
    execution_mode: str,
    base_seq: int,
    event_ts: int,
    bankroll: float = _DEFAULT_BANKROLL,
    broadcast: Callable[[CompetitionEvent], Awaitable[None]] | None = None,
    guards: BreakerCell | None = None,
    now: float | None = None,
) -> list[CompetitionEvent]:
    """Run the policy-gated executor lane over a SEALED run and return derived events.

    For each :class:`~veridex.strategies.value.Proposal` (deterministically ordered), the lane:

    1. Runs the cheap :func:`~veridex.policy.gate.evaluate_pre_quote` pass (kill-switch / sealed-edge /
       stake / venue-market allowlists / order-cap / cooldown / eligibility) BEFORE any venue I/O. A
       pre-quote ``DENIED`` emits a ``phase="pre_quote"`` ``POLICY_RESULT``, rejects the record, and
       SKIPS the venue quote entirely (no network on deny).
    2. Otherwise fetches the venue quote, computes the REAL ``slippage_bps`` (from the proposal's
       sealed ``reference_price``) and the forward ``executable_edge_bps`` at the actual price, runs
       :func:`~veridex.policy.gate.evaluate_post_quote`, and emits a ``phase="post_quote"``
       ``POLICY_RESULT`` carrying those price-dependent numbers (the inert-``slippage_bps=0`` fix).
    3. Creates an :class:`~veridex.execution.models.ExecutionRecord` (``proposed`` →
       ``law_approved``) and branches on the decision:

       * ``DENIED`` (includes ineligible agents) → advance to ``rejected``; NO submit.
       * ``REQUIRES_HUMAN`` → advance to ``awaiting_human``; NO submit (Task-7 resolves it).
       * ``APPROVED`` but ``execution_mode == "paper"`` → leave at ``law_approved``; NO submit.
       * ``APPROVED`` and ``dry_run`` → build a SIMULATED receipt (no ``submit_order`` call).
       * ``APPROVED`` and ``live_guarded`` → real ``submit_order`` → ``get_order_status`` →
         ``normalize_receipt``.

       On a submit (dry_run or live_guarded) the record walks to its receipt terminal and a
       derived ``EXECUTION_SUBMITTED`` + ``EXECUTION_RECEIPT`` pair is emitted.
    4. Persists the record via ``append_execution_record`` (idempotent on a stable
       ``execution_id`` of ``f"{run_id}:{source_sequence_no}"``).

    This function NEVER calls ``run_competition`` and NEVER mutates ``run_result`` — the seal,
    score rows, leaderboard, and Phase-2A evidence events are untouched.

    Args:
        store: Async store; receives one ``append_execution_record`` per proposal.
        competition_id: Owning competition identifier.
        run_result: The frozen, sealed Phase-1 run (read-only).
        envelope: The operator policy envelope (its ``min_edge_bps`` drives selection).
        adapter: The venue adapter (quote/submit/status/normalize).
        entries_by_agent: ``agent_id -> AgentEntry`` roster (supplies ``execution_eligibility``).
        execution_mode: ``"paper"`` | ``"dry_run"`` | ``"live_guarded"``.
        base_seq: First competition ``seq`` to assign; emitted events are contiguous from here.
        event_ts: Deterministic event timestamp stamped on emitted events and used for quote age.
        bankroll: Fixed deterministic bankroll the sealed half-Kelly fraction is sized against.
        broadcast: Optional async hook invoked once per emitted event for live streaming.
        guards: Optional mutable :class:`BreakerCell` threading the T16 circuit-breaker state
            through the run (REQ-2D-404). ``None`` → a transparent ``CLOSED`` breaker (paper/dry and
            any non-opt-in caller are byte-for-byte unaffected). On the live-guarded path ONLY, the
            breaker STATE is threaded into the pre-quote context (an OPEN breaker makes the gate deny
            before any venue I/O — the deny reason is minted inside the gate, never here) and the
            breaker is updated around EXECUTED outcomes (a real fill/failure — never a denial).
        now: Injected clock instant for the breaker's time-based recovery / failure anchoring; the
            breaker NEVER reads a wall clock. Defaults to ``float(event_ts)`` so a run is deterministic.

    Returns:
        The emitted derived :class:`~veridex.competition.events.CompetitionEvent` objects, in
        ascending ``seq`` order. The caller owns appending/broadcasting them to the canonical log.
    """
    proposals = value_proposals(run_result, min_edge_bps=envelope.min_edge_bps)
    venue = _venue_name(adapter)
    events: list[CompetitionEvent] = []
    seq = base_seq
    orders_this_run = 0

    # Live-money safety context (fail-closed). ``live_guarded`` enables the tighter live stake cap;
    # the breaker blocks a live submit when OPEN. Both are inert off the live path.
    live_guarded = execution_mode == _LIVE_GUARDED
    real_venue_quote = _is_real_venue_quote(adapter)
    clock = float(event_ts) if now is None else now
    if guards is not None:
        # Apply the time-based OPEN -> HALF_OPEN recovery ONCE up front (pure; injected clock).
        guards.breaker = guards.breaker.resolve(now=clock, cooldown_s=guards.cooldown_s)

    async def _emit(event: CompetitionEvent) -> None:
        events.append(event)
        if broadcast is not None:
            await broadcast(event)

    for proposal in proposals:
        stake = _size_stake(proposal.kelly_fraction, bankroll=bankroll, max_stake=envelope.max_stake)
        entry = entries_by_agent.get(proposal.agent_id)
        agent_eligible = bool(entry.execution_eligibility) if entry is not None else False

        # --- PRE-QUOTE gate (cheap, deterministic, NO venue I/O) -----------------------
        # Thread the CURRENT breaker state + live-guarded flag so an OPEN breaker (fail-closed) and
        # the tighter live stake cap are enforced BEFORE any venue I/O. The breaker is re-read every
        # iteration, so a mid-run trip (from an executed failure below) blocks the remaining proposals.
        breaker = guards.breaker if guards is not None else CircuitBreaker()
        pre = evaluate_pre_quote(
            PreQuoteContext(
                recomputed_edge_bps=proposal.recomputed_edge_bps,
                stake=stake,
                venue=venue,
                market_key=proposal.market_key,
                orders_this_run=orders_this_run,
                seconds_since_last_order=None,
                agent_eligible=agent_eligible,
                breaker=breaker,
                live_guarded=live_guarded,
            ),
            envelope,
        )

        execution_id = f"{run_result.run_id}:{proposal.source_sequence_no}"
        record = ExecutionRecord(
            execution_id=execution_id,
            competition_id=competition_id,
            run_id=run_result.run_id,
            agent_id=proposal.agent_id,
            source_sequence_no=proposal.source_sequence_no,
            status=ExecutionStatus.PROPOSED,
            policy_hash=pre.policy_hash,
        )
        record.advance(ExecutionStatus.LAW_APPROVED)

        if pre.decision == PolicyDecision.DENIED:
            # Deny BEFORE any venue I/O — the whole point of the pre-quote pass. ``execution_id``
            # stays threaded into the payload so POLICY_OBEYED can still correlate a deny with any
            # (illegal) submit for the same execution (Task-4 bypass detection).
            await _emit(
                build_policy_result_event(
                    competition_id=competition_id,
                    run_id=run_result.run_id,
                    seq=seq,
                    event_ts=event_ts,
                    agent_id=proposal.agent_id,
                    source_sequence_no_ref=proposal.source_sequence_no,
                    policy_result_payload={
                        "decision": pre.decision.value,
                        "reason_codes": list(pre.reason_codes),
                        "policy_hash": pre.policy_hash,
                        "phase": "pre_quote",
                    },
                    execution_id=execution_id,
                )
            )
            seq += 1
            record.advance(ExecutionStatus.REJECTED)
            await store.append_execution_record(record)
            continue  # NO venue quote — saved the I/O.

        # --- venue quote (only reached when the pre-quote gate passes) -----------------
        # QUOTE-SIZE COUPLING (REQ-2D-701 gate 1): price the quote for the SAME ``stake`` the order
        # will submit, so the slippage / executable_edge the post-quote gate acts on reflect the
        # size that actually fills (no size mismatch between the quoted edge and the submitted order).
        quote = await adapter.quote_market(proposal.market_key, for_size=stake)
        slippage = _slippage_bps(proposal.reference_price, quote.price)
        exec_edge = executable_edge_bps(proposal.entry_prob_bps, quote.price)
        # Edge-legibility explanatory quantity (REQ-2D-501): the prob-space dislocation between
        # TxLINE's de-margined fair value and the venue's implied probability. NEVER an edge,
        # NEVER scored — surfaced ALONGSIDE the law's executable_edge for the flagship story.
        gap = mispricing_gap_bps(proposal.entry_prob_bps, quote.price)

        # --- POST-QUOTE gate (REAL slippage + forward executable edge) -----------------
        post = evaluate_post_quote(
            PostQuoteContext(
                executable_edge_bps=exec_edge,
                price=quote.price,
                slippage_bps=slippage,
                quote_age_s=max(0, event_ts - quote.ts),
                stake=stake,
                quoted_size=quote.size,  # book depth feeds the post-quote liquidity guardrail
            ),
            envelope,
        )
        record.policy_hash = post.policy_hash

        # POST-QUOTE POLICY_RESULT — carries the real, price-dependent numbers (the inert-gate fix).
        await _emit(
            build_policy_result_event(
                competition_id=competition_id,
                run_id=run_result.run_id,
                seq=seq,
                event_ts=event_ts,
                agent_id=proposal.agent_id,
                source_sequence_no_ref=proposal.source_sequence_no,
                policy_result_payload={
                    "decision": post.decision.value,
                    "reason_codes": list(post.reason_codes),
                    "policy_hash": post.policy_hash,
                    "phase": "post_quote",
                    "slippage_bps": slippage,
                    "executable_edge_bps": exec_edge,
                    # Edge-legibility fields from the REAL quote (REQ-2D-501) — explanatory,
                    # non-scoring; the display gate only renders them for a real venue quote.
                    "mispricing_gap_bps": gap,
                    "venue_decimal_price": quote.price,
                    "native_price": quote.native_price,
                    # DISPLAY-HONESTY gate (REQ-2D-701 gate 4): EARNED, never inferred. True ONLY
                    # when a GENUINE real-venue adapter produced this quote; Fake/paper/dry → False,
                    # even though the price-dependent numbers above are present. The frontend edge
                    # gate keys on this so an edge NEVER renders without a real venue quote.
                    "real_venue_quote": real_venue_quote,
                },
                execution_id=execution_id,
            )
        )
        seq += 1

        will_submit = post.decision == PolicyDecision.APPROVED and execution_mode != _PAPER and agent_eligible

        if post.decision == PolicyDecision.DENIED:
            record.advance(ExecutionStatus.REJECTED)
        elif post.decision == PolicyDecision.REQUIRES_HUMAN:
            record.advance(ExecutionStatus.AWAITING_HUMAN)
        elif will_submit:
            record.advance(ExecutionStatus.POLICY_APPROVED)
            order = Order(
                market_ref=proposal.market_key,
                side=proposal.side,
                size=stake,
                price=quote.price,
                venue=venue,
                client_order_id=execution_id,
            )
            if execution_mode == _DRY_RUN:
                # SIMULATED: build the status directly — never touch the network/submit path.
                status = OrderStatus(
                    venue_order_id=f"dryrun-{execution_id}",
                    status="filled",
                    filled_size=order.size,
                    price=quote.price,
                )
                receipt = adapter.normalize_receipt(execution_id, order, status, mode=_DRY_RUN)
            else:  # live_guarded
                # Poll until a TERMINAL status (or an honest UNRESOLVED on timeout) — never build a
                # receipt from a transient status, and never fabricate a fill.
                ack = await adapter.submit_order(order)
                status = await poll_order_terminal(adapter, ack.venue_order_id)
                receipt = adapter.normalize_receipt(execution_id, order, status, mode=_LIVE_GUARDED)

            _advance_through(record, _POST_POLICY_PATH.get(receipt.status, (ExecutionStatus.SUBMITTED,)))
            record.receipt = receipt
            orders_this_run += 1

            # BREAKER (REQ-2D-404): move it ONLY on a REAL executed (live_guarded) outcome — a
            # dry_run fill is simulated, not executed, and must never touch the breaker. A rejected /
            # expired / cancelled / UNRESOLVED live receipt is a failure; a FILLED/PARTIAL is a
            # success. (A policy DENY never reaches here, so a denial can never trip the breaker.)
            if guards is not None and execution_mode == _LIVE_GUARDED:
                if receipt.status in _EXECUTED_FAILURE_TERMINALS:
                    guards.breaker = guards.breaker.record_failure(
                        threshold=envelope.circuit_breaker_threshold, now=clock
                    )
                else:
                    guards.breaker = guards.breaker.record_success()

            await _emit(
                build_execution_submitted_event(
                    competition_id=competition_id,
                    run_id=run_result.run_id,
                    seq=seq,
                    event_ts=event_ts,
                    execution_id=execution_id,
                    payload={
                        "execution_id": execution_id,
                        "agent_id": proposal.agent_id,
                        "venue": venue,
                        "market_ref": proposal.market_key,
                        "side": proposal.side,
                        "size": stake,
                        "price": quote.price,
                        "mode": execution_mode,
                    },
                )
            )
            seq += 1
            await _emit(
                build_execution_receipt_event(
                    competition_id=competition_id,
                    run_id=run_result.run_id,
                    seq=seq,
                    event_ts=event_ts,
                    execution_id=execution_id,
                    receipt_payload=receipt.model_dump(mode="json"),
                )
            )
            seq += 1
        # else: APPROVED but paper-mode -> leave at law_approved, no submit, no extra events.

        await store.append_execution_record(record)

    return events


# Decision labels for the human-approval audit (stable wire values).
_APPROVED = "approved"
_REJECTED = "rejected"


async def resolve_approval(
    store: Store,
    *,
    record: ExecutionRecord,
    run_result: RunResult,
    envelope: PolicyEnvelope,
    adapter: VenueAdapter,
    entry: AgentEntry | None,
    execution_mode: str,
    base_seq: int,
    event_ts: int,
    approver_id: str | None,
    note: str | None = None,
    bankroll: float = _DEFAULT_BANKROLL,
    guards: BreakerCell | None = None,
    now: float | None = None,
) -> tuple[ExecutionRecord, list[CompetitionEvent], str]:
    """Resolve an ``awaiting_human`` record: audit, RE-CHECK law+policy+eligibility, submit-or-reject.

    The pending-approval resolution (REQ-2B-19): emit a NON-SCORING approval audit event, then
    independently re-derive the proposal from the SEALED run and re-evaluate the CURRENT policy
    envelope (which may have a flipped kill-switch) plus the agent's current eligibility:

    * If the re-check is clean (no hard reason codes) the record advances
      ``awaiting_human → policy_approved → submitted → …`` and an ``EXECUTION_SUBMITTED`` +
      ``EXECUTION_RECEIPT`` pair is emitted (``dry_run`` simulates; ``live_guarded`` really submits).
    * Otherwise the record advances ``awaiting_human → rejected`` and a denied ``POLICY_RESULT`` is
      emitted — NO submit (fail-closed).

    This NEVER mutates ``run_result`` — the seal, scores, leaderboard, and evidence stay untouched.

    Args:
        store: Async store; receives one ``append_execution_record`` for the resolved record.
        record: The ``awaiting_human`` execution record to resolve (mutated in place).
        run_result: The frozen, sealed Phase-1 run (read-only).
        envelope: The CURRENT operator policy envelope (re-check basis; may have kill_switch on).
        adapter: The venue adapter (quote/submit/status/normalize).
        entry: The agent's roster entry (supplies ``execution_eligibility``); ``None`` → ineligible.
        execution_mode: ``"dry_run"`` | ``"live_guarded"`` (never ``"paper"`` — there is no
            awaiting_human in paper mode).
        base_seq: First competition ``seq`` for the emitted block.
        event_ts: Deterministic event timestamp for emitted events / quote-age math.
        approver_id: The authenticated operator principal recorded in the audit event.
        note: Optional free-form operator note recorded in the audit event.
        bankroll: Fixed deterministic bankroll the sealed half-Kelly fraction is sized against.
        guards: Optional mutable :class:`BreakerCell` (REQ-2D-404); an OPEN breaker denies the
            human-approved live submit (fail-closed) and a live executed outcome moves it. ``None``
            → a transparent ``CLOSED`` breaker (no change for dry_run / non-opt-in callers).
        now: Injected clock instant for the breaker; defaults to ``float(event_ts)`` (never a wall clock).

    Returns:
        ``(updated_record, events, decision)`` where ``decision`` is ``"approved"`` or
        ``"rejected"``. The caller owns appending/broadcasting ``events`` to the canonical log.
    """
    venue = _venue_name(adapter)
    seq = base_seq
    events: list[CompetitionEvent] = []

    # 1. RE-CHECK law: re-derive the proposal for THIS record from the sealed run.
    proposal = next(
        (
            p
            for p in value_proposals(run_result, min_edge_bps=envelope.min_edge_bps)
            if p.source_sequence_no == record.source_sequence_no and p.agent_id == record.agent_id
        ),
        None,
    )

    decision = _REJECTED
    reason_codes: list[str] = []
    result_policy_hash = envelope.policy_hash()
    quote = None
    stake = 0.0

    live_guarded = execution_mode == _LIVE_GUARDED
    clock = float(event_ts) if now is None else now
    if guards is not None:
        guards.breaker = guards.breaker.resolve(now=clock, cooldown_s=guards.cooldown_s)

    if proposal is not None:
        # 2. RE-CHECK policy + eligibility against the CURRENT envelope (two-phase). Thread the same
        # live-money safety context as the main lane — an OPEN breaker + the live stake cap gate the
        # human-approved live submit too (fail-closed; a human approval never bypasses the breaker).
        agent_eligible = bool(entry.execution_eligibility) if entry is not None else False
        stake = _size_stake(proposal.kelly_fraction, bankroll=bankroll, max_stake=envelope.max_stake)
        breaker = guards.breaker if guards is not None else CircuitBreaker()
        pre = evaluate_pre_quote(
            PreQuoteContext(
                recomputed_edge_bps=proposal.recomputed_edge_bps,
                stake=stake,
                venue=venue,
                market_key=proposal.market_key,
                orders_this_run=0,
                seconds_since_last_order=None,
                agent_eligible=agent_eligible,
                breaker=breaker,
                live_guarded=live_guarded,
            ),
            envelope,
        )
        result_policy_hash = pre.policy_hash
        if pre.decision == PolicyDecision.DENIED:
            # Cheap deny (e.g. a flipped kill-switch / OPEN breaker) fails closed BEFORE any venue I/O.
            reason_codes = list(pre.reason_codes)
        else:
            # QUOTE-SIZE COUPLING (gate 1): price the quote for the stake that will submit.
            quote = await adapter.quote_market(proposal.market_key, for_size=stake)
            slippage = _slippage_bps(proposal.reference_price, quote.price)
            exec_edge = executable_edge_bps(proposal.entry_prob_bps, quote.price)
            post = evaluate_post_quote(
                PostQuoteContext(
                    executable_edge_bps=exec_edge,
                    price=quote.price,
                    slippage_bps=slippage,
                    quote_age_s=max(0, event_ts - quote.ts),
                    stake=stake,
                    quoted_size=quote.size,  # book depth feeds the post-quote liquidity guardrail
                ),
                envelope,
            )
            reason_codes = list(post.reason_codes)
            result_policy_hash = post.policy_hash
            # The human is approving an already-clean (or escalated) action: a clean re-check
            # (no hard reason codes — REQUIRES_HUMAN is clean) means APPROVED-and-submit; any hard
            # reason fails closed.
            if not reason_codes:
                decision = _APPROVED
    else:
        reason_codes = ["proposal_no_longer_qualifies"]

    # 3. Emit the NON-SCORING approval audit event (always, regardless of outcome).
    await _emit_audit(events, record, run_result, seq, event_ts, approver_id, note, decision, result_policy_hash)
    seq += 1

    # 4. Branch: submit (approved) or reject (fail-closed).
    if decision == _APPROVED and proposal is not None and quote is not None:
        record.advance(ExecutionStatus.POLICY_APPROVED)
        order = Order(
            market_ref=proposal.market_key,
            side=proposal.side,
            size=stake,
            price=quote.price,
            venue=venue,
            client_order_id=record.execution_id,
        )
        if execution_mode == _DRY_RUN:
            status = OrderStatus(
                venue_order_id=f"dryrun-{record.execution_id}",
                status="filled",
                filled_size=order.size,
                price=quote.price,
            )
            receipt = adapter.normalize_receipt(record.execution_id, order, status, mode=_DRY_RUN)
        else:  # live_guarded
            # Poll until a TERMINAL status (or an honest UNRESOLVED on timeout) — never build a
            # receipt from a transient status, and never fabricate a fill.
            ack = await adapter.submit_order(order)
            status = await poll_order_terminal(adapter, ack.venue_order_id)
            receipt = adapter.normalize_receipt(record.execution_id, order, status, mode=_LIVE_GUARDED)

        _advance_through(record, _POST_POLICY_PATH.get(receipt.status, (ExecutionStatus.SUBMITTED,)))
        record.receipt = receipt

        # BREAKER: move it ONLY on a REAL executed (live_guarded) outcome (see the main lane).
        if guards is not None and execution_mode == _LIVE_GUARDED:
            if receipt.status in _EXECUTED_FAILURE_TERMINALS:
                guards.breaker = guards.breaker.record_failure(
                    threshold=envelope.circuit_breaker_threshold, now=clock
                )
            else:
                guards.breaker = guards.breaker.record_success()

        events.append(
            build_execution_submitted_event(
                competition_id=record.competition_id,
                run_id=run_result.run_id,
                seq=seq,
                event_ts=event_ts,
                execution_id=record.execution_id,
                payload={
                    "execution_id": record.execution_id,
                    "agent_id": record.agent_id,
                    "venue": venue,
                    "market_ref": proposal.market_key,
                    "side": proposal.side,
                    "size": stake,
                    "price": quote.price,
                    "mode": execution_mode,
                },
            )
        )
        seq += 1
        events.append(
            build_execution_receipt_event(
                competition_id=record.competition_id,
                run_id=run_result.run_id,
                seq=seq,
                event_ts=event_ts,
                execution_id=record.execution_id,
                receipt_payload=receipt.model_dump(mode="json"),
            )
        )
        seq += 1
    else:
        record.advance(ExecutionStatus.REJECTED)
        events.append(
            build_policy_result_event(
                competition_id=record.competition_id,
                run_id=run_result.run_id,
                seq=seq,
                event_ts=event_ts,
                agent_id=record.agent_id,
                source_sequence_no_ref=record.source_sequence_no,
                policy_result_payload={
                    "decision": PolicyDecision.DENIED.value,
                    "reason_codes": reason_codes,
                    "policy_hash": result_policy_hash,
                },
                execution_id=record.execution_id,
            )
        )
        seq += 1

    await store.append_execution_record(record)
    return record, events, decision


async def _emit_audit(
    events: list[CompetitionEvent],
    record: ExecutionRecord,
    run_result: RunResult,
    seq: int,
    event_ts: int,
    approver_id: str | None,
    note: str | None,
    decision: str,
    policy_hash: str,
) -> None:
    """Append the REQ-2B-19 non-scoring approval audit event to ``events``."""
    events.append(
        build_approval_audit_event(
            competition_id=record.competition_id,
            run_id=run_result.run_id,
            seq=seq,
            event_ts=event_ts,
            execution_id=record.execution_id,
            audit_payload={
                "approver_id": approver_id,
                "execution_id": record.execution_id,
                "policy_hash": policy_hash,
                "decision": decision,
                "note": note,
                "ts": event_ts,
            },
        )
    )
