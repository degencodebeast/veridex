"""Policy envelope: the operator-set guardrail limits and their canonical hash.

The envelope is the committed configuration the engine evaluates against. Its
``policy_hash`` reuses the ONE canonical serializer (``serialize_payload``) so a policy
commitment is byte-stable across processes and directly comparable to the rest of the
evidence/prescore trust chain.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel

from veridex.runtime.evidence import serialize_payload


def cap_breached(cap: float, loss: float) -> bool:
    """Single source of truth for the R4-A realized-loss cap breach predicate (SAF-002).

    A cap ``<= 0`` is DISABLED (no protection to breach). An ENABLED cap (``> 0``) is breached
    once the accumulated fee-inclusive ``loss`` REACHES it (``>=``): the conservative,
    fail-closed boundary — HALT the moment loss reaches the ceiling rather than admit one more
    order sitting exactly at the maximum. Every loss-cap surface routes through this ONE
    predicate — the pre-quote gate (:func:`veridex.policy.gate.evaluate_pre_quote`), the atomic
    breach-sweep detector (:meth:`veridex.dust_execution.risk.RiskAccumulator.breaches_caps`),
    and the admission decision
    (:meth:`veridex.dust_execution.manifest.StrategyAuthorizationDecision.evaluate`) — so the
    crossing point lives in exactly one place and cannot drift.

    Args:
        cap: The configured loss ceiling; ``<= 0`` disables the cap.
        loss: The accumulated non-negative fee-inclusive realized-loss magnitude.

    Returns:
        ``True`` iff the cap is enabled (``> 0``) AND ``loss >= cap``.
    """
    return cap > 0.0 and loss >= cap


class PolicyEnvelope(BaseModel):
    """Operator-set execution limits gating a single law-approved action.

    Attributes:
        max_stake: Maximum stake allowed on a single order.
        max_orders_per_run: Hard cap on orders placed within one run.
        max_orders_per_session: Hard cap on orders placed within one session.
        max_orders_per_day: Hard cap on orders placed within one day.
        venue_allowlist: Venues an order may be routed to.
        market_allowlist: Market keys an order may target.
        min_edge_bps: Minimum recomputed edge (basis points) required to act.
        max_slippage_bps: Maximum tolerated slippage (basis points).
        max_price: Maximum acceptable order price.
        max_quote_age_s: Maximum acceptable quote age (seconds) before it is stale.
        cooldown_s: Minimum seconds that must elapse between consecutive orders.
        human_approval_threshold: Stake at or above which a clean action escalates
            to human approval instead of auto-approving.
        max_stake_live_guarded: Tighter per-order stake cap that applies ONLY on the
            live-guarded (real-money) path; ``<= 0`` leaves ``max_stake`` as the only cap.
        circuit_breaker_threshold: Consecutive execution failures that trip the circuit
            breaker OPEN (blocking further execution); ``<= 0`` disables the breaker.
        max_session_loss: Fee-inclusive realized-loss ceiling for one execution session;
            ``<= 0`` disables the cap (mirrors ``max_stake_live_guarded``). SAF-002: Mode B
            (real money) admission REQUIRES a finite positive value; a disabled cap is
            permitted ONLY in non-money modes.
        max_daily_loss: Fee-inclusive realized-loss ceiling for one UTC day; ``<= 0``
            disables the cap. Same Mode B admission requirement as ``max_session_loss``.
        kill_switch: When true, denies everything unconditionally.
    """

    max_stake: float
    max_orders_per_run: int
    max_orders_per_session: int
    max_orders_per_day: int
    venue_allowlist: list[str]
    market_allowlist: list[str]
    min_edge_bps: int
    max_slippage_bps: int
    max_price: float
    max_quote_age_s: int
    cooldown_s: int
    human_approval_threshold: float
    max_stake_live_guarded: float = 0.0
    circuit_breaker_threshold: int = 0
    max_session_loss: float = 0.0
    max_daily_loss: float = 0.0
    kill_switch: bool = False

    def policy_hash(self) -> str:
        """Return the deterministic SHA-256 of the canonically-serialized envelope.

        Returns:
            Hex-encoded SHA-256 digest over ``serialize_payload(self.model_dump())``,
            stable across processes for the same field values.
        """
        return hashlib.sha256(serialize_payload(self.model_dump()).encode("utf-8")).hexdigest()
