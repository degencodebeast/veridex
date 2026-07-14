"""Deny-by-default policy engine: pure, sync, LLM-free evaluation of a single action.

``evaluate`` checks a law-approved action against an operator ``PolicyEnvelope`` and
returns a decision plus the FULL list of failing reason codes (it never short-circuits).
Any hard failure denies; a clean action at or above the human-approval threshold
escalates; otherwise it is approved.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from veridex.policy.envelope import PolicyEnvelope

# Reason codes (exact wire values; named literals so callers/tests reference one source).
_REASON_KILL_SWITCH_ON = "kill_switch_on"
_REASON_EDGE_BELOW_MIN = "edge_below_min"
_REASON_QUOTE_STALE = "quote_stale"
_REASON_STAKE_OVER_MAX = "stake_over_max"
_REASON_VENUE_NOT_ALLOWED = "venue_not_allowed"
_REASON_MARKET_NOT_ALLOWED = "market_not_allowed"
_REASON_SLIPPAGE_OVER_MAX = "slippage_over_max"
_REASON_PRICE_OVER_MAX = "price_over_max"
_REASON_ORDER_CAP_RUN = "order_cap_run"
_REASON_COOLDOWN_ACTIVE = "cooldown_active"
_REASON_AGENT_NOT_ELIGIBLE = "agent_not_eligible"
_REASON_MISSING_FIELD = "missing_field"
# Guardrail lift (REQ-2D-404/405): breaker (cheap/pre-quote) + liquidity (quote-dependent/post-quote)
# + the tighter live-money stake cap (cheap/pre-quote). All are ordinary deny reasons inside the
# ONE policy gate — never a second authority.
_REASON_CIRCUIT_OPEN = "circuit_open"
_REASON_INSUFFICIENT_LIQUIDITY = "insufficient_liquidity"
_REASON_STAKE_OVER_LIVE_GUARDED = "stake_over_live_guarded"
# R4-A dust-execution safety (SAF-002): fee-inclusive realized-loss ceilings (session/day) and
# the now-WIRED per-session/per-day order caps (previously declared-but-dead on PolicyEnvelope).
# All are ordinary deny reasons inside the ONE policy gate — never a second authority.
_REASON_SESSION_LOSS_OVER_MAX = "session_loss_over_max"
_REASON_DAILY_LOSS_OVER_MAX = "daily_loss_over_max"
_REASON_ORDER_CAP_SESSION = "order_cap_session"
_REASON_ORDER_CAP_DAY = "order_cap_day"


class PolicyDecision(str, Enum):
    """Terminal verdict for a single action.

    Attributes:
        APPROVED: Action may execute with no further gating.
        DENIED: Action is blocked; see ``reason_codes``.
        REQUIRES_HUMAN: Action is clean but escalated for human approval.
    """

    APPROVED = "approved"
    DENIED = "denied"
    REQUIRES_HUMAN = "requires_human"


class PolicyContext(BaseModel):
    """Recomputed, trust-path facts about the action under evaluation.

    Attributes:
        recomputed_edge_bps: Independently recomputed edge in basis points.
        stake: Proposed stake for the order.
        venue: Target venue identifier.
        market_key: Target market key.
        price: Proposed order price.
        slippage_bps: Estimated slippage in basis points.
        quote_age_s: Age of the quote backing this action, in seconds.
        orders_this_run: Orders already placed in the current run.
        seconds_since_last_order: Seconds since the previous order, or ``None`` if
            no prior order exists in scope (cooldown cannot apply).
        agent_eligible: Whether the agent is currently eligible to execute.
    """

    recomputed_edge_bps: int
    stake: float
    venue: str
    market_key: str
    price: float
    slippage_bps: int
    quote_age_s: int
    orders_this_run: int
    seconds_since_last_order: int | None
    agent_eligible: bool


class PolicyResult(BaseModel):
    """Outcome of ``evaluate``.

    Attributes:
        decision: The terminal verdict.
        reason_codes: All failing reason codes (empty for a clean approval).
        policy_hash: Hash of the envelope the decision was made against.
    """

    decision: PolicyDecision
    reason_codes: list[str]
    policy_hash: str


def evaluate(ctx: PolicyContext, envelope: PolicyEnvelope) -> PolicyResult:
    """Evaluate ``ctx`` against ``envelope`` deny-by-default, collecting all failures.

    Every hard rule is checked (no short-circuit) so the result lists every reason an
    action was blocked. If any hard reason is present the decision is ``DENIED``;
    otherwise a stake at or above ``human_approval_threshold`` yields ``REQUIRES_HUMAN``
    and anything else is ``APPROVED``.

    Args:
        ctx: Recomputed facts about the action.
        envelope: Operator-set guardrail limits.

    Returns:
        A ``PolicyResult`` carrying the decision, all reason codes, and the policy hash.
    """
    reason_codes: list[str] = []

    if envelope.kill_switch:
        reason_codes.append(_REASON_KILL_SWITCH_ON)
    if ctx.recomputed_edge_bps < envelope.min_edge_bps:
        reason_codes.append(_REASON_EDGE_BELOW_MIN)
    if ctx.quote_age_s > envelope.max_quote_age_s:
        reason_codes.append(_REASON_QUOTE_STALE)
    if ctx.stake > envelope.max_stake:
        reason_codes.append(_REASON_STAKE_OVER_MAX)
    if ctx.venue not in envelope.venue_allowlist:
        reason_codes.append(_REASON_VENUE_NOT_ALLOWED)
    if ctx.market_key not in envelope.market_allowlist:
        reason_codes.append(_REASON_MARKET_NOT_ALLOWED)
    if ctx.slippage_bps > envelope.max_slippage_bps:
        reason_codes.append(_REASON_SLIPPAGE_OVER_MAX)
    if ctx.price > envelope.max_price:
        reason_codes.append(_REASON_PRICE_OVER_MAX)
    if ctx.orders_this_run >= envelope.max_orders_per_run:
        reason_codes.append(_REASON_ORDER_CAP_RUN)
    if ctx.seconds_since_last_order is not None and ctx.seconds_since_last_order < envelope.cooldown_s:
        reason_codes.append(_REASON_COOLDOWN_ACTIVE)
    if not ctx.agent_eligible:
        reason_codes.append(_REASON_AGENT_NOT_ELIGIBLE)

    policy_hash = envelope.policy_hash()

    if reason_codes:
        decision = PolicyDecision.DENIED
    elif ctx.stake >= envelope.human_approval_threshold:
        decision = PolicyDecision.REQUIRES_HUMAN
    else:
        decision = PolicyDecision.APPROVED

    return PolicyResult(decision=decision, reason_codes=reason_codes, policy_hash=policy_hash)
