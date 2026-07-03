"""S5 — ValueVsVenue: fair (TxLINE) vs an INJECTED venue quote → an ESTIMATED executable edge.

Where momentum/drift read only TxLINE, this agent compares TxLINE's de-margined fair probability
against a REAL venue decimal price to surface an ESTIMATED executable edge — the EV of taking the
consensus probability at the price actually on offer. Two trust boundaries are load-bearing:

  * **Venue data enters ONLY through the injected ``venue_price_source`` (SEC-003).** The agent
    NEVER reads a venue price out of ``market_state`` (which is sealed into the evidence hash); the
    price arrives via a caller-supplied ``Callable[[str], float | None]`` keyed by market_key.

  * **The action smuggles NO venue value into evidence (INVARIANT 4).** ``AgentAction.model_dump()``
    is serialized into the sealed ``evidence_hash`` (orchestrator), so the emitted ``params`` carry
    ONLY TxLINE-derived fields (market_key/side + a STATIC, number-free ``reason``). The estimated
    edge / gap / venue price are DELIBERATELY absent from the action — they are the producer's job
    to attach to a report POST-build (Task 18b), never the law-sealed decision's.

PROPOSER ONLY (gate 1): it emits ``FOLLOW_MOMENTUM``; the deterministic law scores edge/CLV. The
estimated executable edge is EXPLANATORY (SEC-005) — it is NEVER a ranked axis. This module imports
NO LLM SDK. ``vvv_signal`` is a pure, sync core; the agent is a real reproducible-proof Agent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from veridex.execution.legibility import mispricing_gap_bps
from veridex.ingest.marketstate import MarketState
from veridex.law.edge import executable_edge_bps
from veridex.runtime.agent import AGENT_ACTION_SCHEMA_VERSION, agent_config_hash
from veridex.runtime.orchestrator import PROOF_MODE_REPRODUCIBLE, Agent
from veridex.runtime.schemas import AgentAction, SportsActionType

#: STATIC, number-free rationale — carries NO venue-derived value into the sealed evidence (INV 4).
_VVV_REASON = "value vs venue: estimated executable edge cleared the minimum"


def vvv_signal(
    fair_prob_bps: int, venue_decimal_price: float | None, *, min_edge_bps: int = 0
) -> dict[str, Any]:
    """Pure core: fair prob vs a venue DECIMAL price → gap + estimated executable edge + fired.

    No quote (``venue_decimal_price is None``) ⇒ no edge: both edge fields are ``None`` and the
    signal does not fire (fail-safe — there is nothing to estimate against).

    Args:
        fair_prob_bps: TxLINE de-margined consensus fair probability for the side, in bps.
        venue_decimal_price: The venue's DECIMAL odds (``None`` when no quote is available). MUST be
            a decimal price — convert a native share price via
            :func:`veridex.venues.polymarket.native_to_decimal` before calling.
        min_edge_bps: Minimum estimated executable edge (bps) required to fire.

    Returns:
        ``{"gap_bps": int | None, "estimated_executable_edge_bps": int | None, "fired": bool}``.
        ``fired`` is ``True`` iff an edge was computable AND ``>= min_edge_bps``.

    Raises:
        ValueError: If ``venue_decimal_price`` is not ``None`` and ``<= 1.0`` — decimal odds are
            ``1/q`` and thus ALWAYS ``> 1.0``, so a value ``<= 1.0`` is a native-q (share price)
            passed where decimal odds are required. Fail fast at the boundary (AC-014 lesson from
            M0) rather than silently mispricing to an all-negative edge that never fires.
    """
    if venue_decimal_price is None:
        return {"gap_bps": None, "estimated_executable_edge_bps": None, "fired": False}
    if venue_decimal_price <= 1.0:
        raise ValueError(
            f"venue_decimal_price must be DECIMAL odds (> 1.0), got {venue_decimal_price!r} — this "
            "looks like a native share price q; convert via veridex.venues.polymarket.native_to_decimal"
        )
    gap_bps = mispricing_gap_bps(fair_prob_bps, venue_decimal_price)
    estimated_edge_bps = executable_edge_bps(fair_prob_bps, venue_decimal_price)
    fired = estimated_edge_bps is not None and estimated_edge_bps >= min_edge_bps
    return {"gap_bps": gap_bps, "estimated_executable_edge_bps": estimated_edge_bps, "fired": fired}


def value_vs_venue_agent(
    *,
    venue_price_source: Callable[[str], float | None],
    min_edge_bps: int = 0,
    agent_id: str = "value-vs-venue",
) -> Agent:
    """Build a reproducible-proof ValueVsVenue contestant for the orchestrator.

    The venue price is read ONLY from ``venue_price_source(market_key)`` — NEVER from the sealed
    ``market_state`` (SEC-003). The emitted action carries ONLY TxLINE-derived params (INVARIANT 4):
    market_key/side + a static reason; the venue-derived estimated edge/gap NEVER ride into evidence.

    Args:
        venue_price_source: Injected ``Callable[[str], float | None]`` returning the venue DECIMAL
            price for a market_key (``None`` when no quote is available).
        min_edge_bps: Minimum estimated executable edge (bps) required to fire.
        agent_id: Identifier for this agent.

    Returns:
        An :class:`~veridex.runtime.orchestrator.Agent` whose ``proof_mode`` is ``"reproducible"``.
    """

    async def decide(market_state: MarketState) -> AgentAction:
        markets: dict[str, dict[str, Any]] = getattr(market_state, "markets", {}) or {}
        for market_key in sorted(markets):
            market = markets[market_key]
            if market.get("suspended"):
                continue
            prob_bps = market.get("stable_prob_bps", {})
            if not isinstance(prob_bps, dict):
                continue
            # Venue price comes ONLY from the injected source — never from market_state.
            venue_decimal_price = venue_price_source(market_key)
            for side in sorted(prob_bps):
                try:
                    fair_prob_bps = int(prob_bps[side])
                except (TypeError, ValueError):
                    continue
                signal = vvv_signal(fair_prob_bps, venue_decimal_price, min_edge_bps=min_edge_bps)
                if signal["fired"]:
                    # params carry ONLY TxLINE-derived fields — NO venue value enters the seal (INV 4).
                    return AgentAction(
                        type=SportsActionType.FOLLOW_MOMENTUM,
                        params={"market_key": market_key, "side": side, "reason": _VVV_REASON},
                    )
        return AgentAction(type=SportsActionType.WAIT, params={})

    def config_hash(market_state: MarketState) -> str:
        # min_edge_bps is the only behavioural param → same config ⇒ same sealed identity.
        return agent_config_hash(
            agent_id,
            f"value_vs_venue:min_edge_bps={min_edge_bps}",
            AGENT_ACTION_SCHEMA_VERSION,
        )

    return Agent(agent_id=agent_id, proof_mode=PROOF_MODE_REPRODUCIBLE, decide=decide, config_hash=config_hash)
