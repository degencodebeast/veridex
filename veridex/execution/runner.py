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
from veridex.execution.models import ExecutionRecord, ExecutionStatus
from veridex.policy.engine import PolicyContext, PolicyDecision, evaluate
from veridex.strategies.value import value_proposals
from veridex.venues.base import Order, OrderStatus

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
) -> list[CompetitionEvent]:
    """Run the policy-gated executor lane over a SEALED run and return derived events.

    For each :class:`~veridex.strategies.value.Proposal` (deterministically ordered), the lane:

    1. Fetches a venue quote and builds a :class:`~veridex.policy.engine.PolicyContext` from the
       SEALED proposal edge plus the quote and operator-set stake/eligibility facts.
    2. Evaluates the deny-by-default policy and emits a derived ``POLICY_RESULT`` event.
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

    Returns:
        The emitted derived :class:`~veridex.competition.events.CompetitionEvent` objects, in
        ascending ``seq`` order. The caller owns appending/broadcasting them to the canonical log.
    """
    proposals = value_proposals(run_result, min_edge_bps=envelope.min_edge_bps)
    venue = _venue_name(adapter)
    events: list[CompetitionEvent] = []
    seq = base_seq
    orders_this_run = 0

    async def _emit(event: CompetitionEvent) -> None:
        events.append(event)
        if broadcast is not None:
            await broadcast(event)

    for proposal in proposals:
        quote = await adapter.quote_market(proposal.market_key)
        stake = _size_stake(proposal.kelly_fraction, bankroll=bankroll, max_stake=envelope.max_stake)
        entry = entries_by_agent.get(proposal.agent_id)
        agent_eligible = bool(entry.execution_eligibility) if entry is not None else False

        ctx = PolicyContext(
            recomputed_edge_bps=proposal.recomputed_edge_bps,
            stake=stake,
            venue=venue,
            market_key=proposal.market_key,
            price=quote.price,
            slippage_bps=0,
            quote_age_s=max(0, event_ts - quote.ts),
            orders_this_run=orders_this_run,
            seconds_since_last_order=None,
            agent_eligible=agent_eligible,
        )
        result = evaluate(ctx, envelope)

        execution_id = f"{run_result.run_id}:{proposal.source_sequence_no}"
        record = ExecutionRecord(
            execution_id=execution_id,
            competition_id=competition_id,
            run_id=run_result.run_id,
            agent_id=proposal.agent_id,
            source_sequence_no=proposal.source_sequence_no,
            status=ExecutionStatus.PROPOSED,
            policy_hash=result.policy_hash,
        )
        record.advance(ExecutionStatus.LAW_APPROVED)

        # POLICY_RESULT — one per proposal, always emitted, derived/non-evidence.
        await _emit(
            build_policy_result_event(
                competition_id=competition_id,
                run_id=run_result.run_id,
                seq=seq,
                event_ts=event_ts,
                agent_id=proposal.agent_id,
                source_sequence_no_ref=proposal.source_sequence_no,
                policy_result_payload={
                    "decision": result.decision.value,
                    "reason_codes": list(result.reason_codes),
                    "policy_hash": result.policy_hash,
                },
            )
        )
        seq += 1

        will_submit = result.decision == PolicyDecision.APPROVED and execution_mode != _PAPER and agent_eligible

        if result.decision == PolicyDecision.DENIED:
            record.advance(ExecutionStatus.REJECTED)
        elif result.decision == PolicyDecision.REQUIRES_HUMAN:
            record.advance(ExecutionStatus.AWAITING_HUMAN)
        elif will_submit:
            record.advance(ExecutionStatus.POLICY_APPROVED)
            order = Order(
                market_ref=proposal.market_key,
                side=proposal.side,
                size=stake,
                price=quote.price,
                venue=venue,
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
                ack = await adapter.submit_order(order)
                status = await adapter.get_order_status(ack.venue_order_id)
                receipt = adapter.normalize_receipt(execution_id, order, status, mode=_LIVE_GUARDED)

            _advance_through(record, _POST_POLICY_PATH.get(receipt.status, (ExecutionStatus.SUBMITTED,)))
            record.receipt = receipt
            orders_this_run += 1

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

    if proposal is not None:
        # 2. RE-CHECK policy + eligibility against the CURRENT envelope.
        quote = await adapter.quote_market(proposal.market_key)
        stake = _size_stake(proposal.kelly_fraction, bankroll=bankroll, max_stake=envelope.max_stake)
        agent_eligible = bool(entry.execution_eligibility) if entry is not None else False
        ctx = PolicyContext(
            recomputed_edge_bps=proposal.recomputed_edge_bps,
            stake=stake,
            venue=venue,
            market_key=proposal.market_key,
            price=quote.price,
            slippage_bps=0,
            quote_age_s=max(0, event_ts - quote.ts),
            orders_this_run=0,
            seconds_since_last_order=None,
            agent_eligible=agent_eligible,
        )
        result = evaluate(ctx, envelope)
        reason_codes = list(result.reason_codes)
        result_policy_hash = result.policy_hash
        # The human is approving an already-clean (or escalated) action: a clean re-check
        # (no hard reason codes) means APPROVED-and-submit; any hard reason fails closed.
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
            ack = await adapter.submit_order(order)
            status = await adapter.get_order_status(ack.venue_order_id)
            receipt = adapter.normalize_receipt(record.execution_id, order, status, mode=_LIVE_GUARDED)

        _advance_through(record, _POST_POLICY_PATH.get(receipt.status, (ExecutionStatus.SUBMITTED,)))
        record.receipt = receipt

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
