"""S5 — ValueVsVenue: fair (TxLINE) vs an INJECTED venue quote → an ESTIMATED executable edge.

Where momentum/drift read only TxLINE, this agent compares TxLINE's de-margined fair probability
against a REAL venue decimal price to surface an ESTIMATED executable edge — the EV of taking the
consensus probability at the price actually on offer. Two trust boundaries are load-bearing:

  * **Venue data enters ONLY through the injected ``venue_price_source`` (SEC-003).** The agent
    NEVER reads a venue price out of ``market_state`` (which is sealed into the evidence hash); the
    price arrives via a caller-supplied :data:`~veridex.venues.venue_price_source.VenuePriceSource`
    — TIME-ALIGNED: keyed by the decision coordinate ``(fixture_id, market_key, side, ts)`` so the
    quote priced against is the one on offer AT that tick, returning ``None`` (⇒ WAIT) when there is
    no quote at/under the caller's freshness bound (CON-006).

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

from typing import Any

from veridex.execution.legibility import mispricing_gap_bps
from veridex.ingest.marketstate import MarketState
from veridex.law.edge import executable_edge_bps
from veridex.runtime.agent import AGENT_ACTION_SCHEMA_VERSION, agent_config_hash
from veridex.runtime.orchestrator import PROOF_MODE_REPRODUCIBLE, Agent
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.venues.venue_price_source import VenuePriceSource, txline_market_to_venue_ref

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
    venue_price_source: VenuePriceSource,
    venue_source_id: str,
    min_edge_bps: int = 0,
    agent_id: str = "value-vs-venue",
) -> Agent:
    """Build a reproducible-proof ValueVsVenue contestant for the orchestrator.

    The venue price is read ONLY from the time-aligned
    ``venue_price_source(fixture_id, market_key, side, ts)`` — NEVER from the sealed ``market_state``
    (SEC-003). The emitted action carries ONLY TxLINE-derived params (INVARIANT 4): market_key/side +
    a static reason; the venue-derived estimated edge/gap NEVER ride into evidence.

    REPRODUCIBILITY (Codex M6): because ``decide`` reads ``venue_price_source`` — which changes the
    agent's sealed actions (fire vs wait) — the venue source's IDENTITY is a behaviour-determining
    input and MUST be pinned in ``config_hash``. ``venue_source_id`` is that identity (an artifact
    hash/name, NOT a price): folding it into the config hash makes "same config + same TxLINE
    evidence ⇒ same decision" true again. It is a CONFIG-HASH input ONLY — it NEVER enters
    ``AgentAction.params`` (identity, not venue data), so the no-smuggling boundary is untouched.

    Args:
        venue_price_source: Injected time-aligned
            :data:`~veridex.venues.venue_price_source.VenuePriceSource` — called with
            ``(fixture_id, market_key, side, ts)`` and returning a
            :class:`~veridex.venues.venue_price_source.TimedVenueQuote` (whose ``venue_decimal_price``
            the agent prices against) or ``None`` when no quote is available (⇒ the agent WAITs).
        venue_source_id: A stable identity for the venue price source (in production, the
            content/artifact hash of the quote/price-history pack it prices against). Bound into
            ``config_hash`` for reproducibility. Must be non-empty.
        min_edge_bps: Minimum estimated executable edge (bps) required to fire.
        agent_id: Identifier for this agent.

    Returns:
        An :class:`~veridex.runtime.orchestrator.Agent` whose ``proof_mode`` is ``"reproducible"``.

    Raises:
        ValueError: If ``venue_source_id`` is empty/falsy — a reproducible agent cannot have a
            behaviour-determining venue source with no identity to pin into its config hash.
    """
    if not venue_source_id:
        raise ValueError(
            "venue_source_id must be a non-empty identity for the venue price source — a reproducible "
            "VvV agent's config_hash must pin the (behaviour-determining) venue source it prices against"
        )

    async def decide(market_state: MarketState) -> AgentAction:
        markets: dict[str, dict[str, Any]] = getattr(market_state, "markets", {}) or {}
        for market_key in sorted(markets):
            market = markets[market_key]
            if market.get("suspended"):
                continue
            prob_bps = market.get("stable_prob_bps", {})
            if not isinstance(prob_bps, dict):
                continue
            for side in sorted(prob_bps):
                try:
                    fair_prob_bps = int(prob_bps[side])
                except (TypeError, ValueError):
                    continue
                # Bridge the TxLINE (market_key, side) to the C-3 frame ``market_ref`` the source is keyed
                # by. ``None`` ⇒ out of venue scope (no frame for this market — AH / OU / 1X2-half): skip
                # the lookup (this side cannot be priced against the venue → no edge → does not fire).
                venue_ref = txline_market_to_venue_ref(market_key, side)
                if venue_ref is None:
                    continue
                # Venue price comes ONLY from the injected time-aligned source (per fixture/market/side/ts)
                # — never from market_state. No quote (None) ⇒ no edge ⇒ this side does not fire (WAIT).
                # The source is keyed by unix SECONDS; MarketState.ts is ALREADY unix seconds (the
                # normalizer emits seconds), so it is passed through directly — no conversion.
                quote = venue_price_source(
                    market_state.fixture_id, venue_ref, side, market_state.ts
                )
                venue_decimal_price = quote.venue_decimal_price if quote is not None else None
                signal = vvv_signal(fair_prob_bps, venue_decimal_price, min_edge_bps=min_edge_bps)
                if signal["fired"]:
                    # params carry ONLY TxLINE-derived fields — NO venue value enters the seal (INV 4).
                    return AgentAction(
                        type=SportsActionType.FOLLOW_MOMENTUM,
                        params={"market_key": market_key, "side": side, "reason": _VVV_REASON},
                    )
        return AgentAction(type=SportsActionType.WAIT, params={})

    def config_hash(market_state: MarketState) -> str:
        # Behaviour-determining inputs = min_edge_bps AND the venue source identity (venue_source_id):
        # both change the sealed decision, so both enter the hash → same config ⇒ same sealed identity.
        return agent_config_hash(
            agent_id,
            f"value_vs_venue:min_edge_bps={min_edge_bps}:venue_source_id={venue_source_id}",
            AGENT_ACTION_SCHEMA_VERSION,
        )

    return Agent(agent_id=agent_id, proof_mode=PROOF_MODE_REPRODUCIBLE, decide=decide, config_hash=config_hash)
