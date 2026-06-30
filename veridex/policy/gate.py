"""Aegis two-phase policy gate (master-plan Pre-2C): split deny-by-default evaluation around the
venue quote so cheap deterministic limits reject BEFORE any network I/O, and price-dependent
limits reject AFTER a quote (and before submit).

This fixes the inert slippage gate: the single-pass ``engine.evaluate`` was fed
``slippage_bps=0`` by the runner, so the slippage rule never fired. Here the post-quote pass takes
a real ``slippage_bps`` + ``executable_edge_bps`` computed from the live quote.

Trust path (CON-007): pure, sync, deny-by-default, LLM-free. Reuses the engine's reason-code
literals — no new ``PolicyEnvelope`` fields, so ``policy_hash`` is unchanged.
"""

from __future__ import annotations

from pydantic import BaseModel

from veridex.policy.engine import (
    _REASON_AGENT_NOT_ELIGIBLE,
    _REASON_COOLDOWN_ACTIVE,
    _REASON_EDGE_BELOW_MIN,
    _REASON_KILL_SWITCH_ON,
    _REASON_MARKET_NOT_ALLOWED,
    _REASON_ORDER_CAP_RUN,
    _REASON_PRICE_OVER_MAX,
    _REASON_QUOTE_STALE,
    _REASON_SLIPPAGE_OVER_MAX,
    _REASON_STAKE_OVER_MAX,
    _REASON_VENUE_NOT_ALLOWED,
    PolicyDecision,
    PolicyResult,
)
from veridex.policy.envelope import PolicyEnvelope


class PreQuoteContext(BaseModel):
    """Cheap, deterministic facts known BEFORE any venue quote/network I/O.

    Attributes:
        recomputed_edge_bps: Sealed deterministic-law edge (bps) — a cheap pre-screen.
        stake: Proposed stake (cap check).
        venue: Target venue slug.
        market_key: Target market key.
        orders_this_run: Orders already placed this run.
        seconds_since_last_order: Seconds since the previous order, or ``None``.
        agent_eligible: Whether the agent is cleared to execute.
    """

    recomputed_edge_bps: int
    stake: float
    venue: str
    market_key: str
    orders_this_run: int
    seconds_since_last_order: int | None
    agent_eligible: bool


class PostQuoteContext(BaseModel):
    """Price-dependent facts known only AFTER a venue quote.

    Attributes:
        executable_edge_bps: Forward edge (bps) at the actual quoted price.
        price: The executable decimal price.
        slippage_bps: Deviation (bps) of the quote from the sealed reference price.
        quote_age_s: Age of the quote in seconds.
        stake: Final stake (exposure + human-approval threshold).
    """

    executable_edge_bps: int
    price: float
    slippage_bps: int
    quote_age_s: int
    stake: float


def evaluate_pre_quote(ctx: PreQuoteContext, envelope: PolicyEnvelope) -> PolicyResult:
    """Phase 1 — cheap deny-by-default checks BEFORE any venue I/O (collect all reasons)."""
    reasons: list[str] = []
    if envelope.kill_switch:
        reasons.append(_REASON_KILL_SWITCH_ON)
    if ctx.recomputed_edge_bps < envelope.min_edge_bps:
        reasons.append(_REASON_EDGE_BELOW_MIN)
    if ctx.stake > envelope.max_stake:
        reasons.append(_REASON_STAKE_OVER_MAX)
    if ctx.venue not in envelope.venue_allowlist:
        reasons.append(_REASON_VENUE_NOT_ALLOWED)
    if ctx.market_key not in envelope.market_allowlist:
        reasons.append(_REASON_MARKET_NOT_ALLOWED)
    if ctx.orders_this_run >= envelope.max_orders_per_run:
        reasons.append(_REASON_ORDER_CAP_RUN)
    if ctx.seconds_since_last_order is not None and ctx.seconds_since_last_order < envelope.cooldown_s:
        reasons.append(_REASON_COOLDOWN_ACTIVE)
    if not ctx.agent_eligible:
        reasons.append(_REASON_AGENT_NOT_ELIGIBLE)
    decision = PolicyDecision.DENIED if reasons else PolicyDecision.APPROVED
    return PolicyResult(decision=decision, reason_codes=reasons, policy_hash=envelope.policy_hash())


def evaluate_post_quote(ctx: PostQuoteContext, envelope: PolicyEnvelope) -> PolicyResult:
    """Phase 2 — price-dependent deny-by-default checks AFTER a quote, before submit.

    A clean pass with ``stake >= human_approval_threshold`` escalates to ``REQUIRES_HUMAN``.
    """
    reasons: list[str] = []
    if ctx.quote_age_s > envelope.max_quote_age_s:
        reasons.append(_REASON_QUOTE_STALE)
    if ctx.slippage_bps > envelope.max_slippage_bps:
        reasons.append(_REASON_SLIPPAGE_OVER_MAX)
    if ctx.executable_edge_bps < envelope.min_edge_bps:
        reasons.append(_REASON_EDGE_BELOW_MIN)
    if ctx.price > envelope.max_price:
        reasons.append(_REASON_PRICE_OVER_MAX)
    if ctx.stake > envelope.max_stake:
        reasons.append(_REASON_STAKE_OVER_MAX)
    if reasons:
        decision = PolicyDecision.DENIED
    elif ctx.stake >= envelope.human_approval_threshold:
        decision = PolicyDecision.REQUIRES_HUMAN
    else:
        decision = PolicyDecision.APPROVED
    return PolicyResult(decision=decision, reason_codes=reasons, policy_hash=envelope.policy_hash())
