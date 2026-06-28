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
    kill_switch: bool = False

    def policy_hash(self) -> str:
        """Return the deterministic SHA-256 of the canonically-serialized envelope.

        Returns:
            Hex-encoded SHA-256 digest over ``serialize_payload(self.model_dump())``,
            stable across processes for the same field values.
        """
        return hashlib.sha256(serialize_payload(self.model_dump()).encode("utf-8")).hexdigest()
