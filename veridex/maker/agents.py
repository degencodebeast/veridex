"""Proposal-only market-maker agents.

These agents implement the `MarketMakerAgent` protocol from
`veridex.maker.contracts`: given a snapshot of reference fair value, venue
view, inventory, and params, they *propose* a `TargetQuoteSet` — a target
quote ladder to reconcile toward. They never place orders directly and never
read a live feed; all state is injected via `propose(...)` arguments. This
keeps them pure and deterministic, and keeps the maker loop out of the
directional decision path (no import of `veridex.runtime.orchestrator`).
"""

from __future__ import annotations

from typing import Any

from veridex.maker.contracts import Side, TargetQuote, TargetQuoteSet
from veridex.maker.state_machine import GateContext, MakerState, classify_state

_MARKET_KEY = "1X2|home|full"


class NaiveMarketMakerAgent:
    """Quotes a fixed half-spread symmetrically around the venue's own mid.

    No gates, no skew — a naive baseline strategy that anchors purely on
    `venue_view["mid"]`, ignoring any reference fair value.
    """

    def __init__(
        self,
        agent_id: str = "naive-mm",
        anchor: str = "venue_mid",
        fixed_half_spread: float = 0.02,
    ) -> None:
        self.agent_id = agent_id
        self.anchor = anchor
        self.fixed_half_spread = fixed_half_spread

    def propose(
        self,
        *,
        reference_fv: dict[str, Any],
        venue_view: dict[str, Any],
        inventory: dict[str, Any],
        params: dict[str, Any],
        clock: int,
    ) -> TargetQuoteSet:
        mid = venue_view["mid"]
        bid = TargetQuote(
            side=Side.BID,
            market_key=_MARKET_KEY,
            price=round(mid - self.fixed_half_spread, 4),
            size=1.0,
        )
        ask = TargetQuote(
            side=Side.ASK,
            market_key=_MARKET_KEY,
            price=round(mid + self.fixed_half_spread, 4),
            size=1.0,
        )
        return TargetQuoteSet(fixture_id=0, tick_seq=0, ts=clock, quotes=[bid, ask])

    def params_hash_inputs(self) -> str:
        return f"naive:anchor={self.anchor}:hs={self.fixed_half_spread}"


class TxLineFairMarketMakerAgent:
    """Quotes a fixed half-spread around TxLINE's reference fair value.

    Anchors on `reference_fv["fv"]`, ignoring `venue_view["mid"]` entirely.
    Abstains (NO_QUOTE — empty quote list) when fair value is unavailable or
    the reference feed is flagged suspended.
    """

    def __init__(
        self,
        agent_id: str = "txline-fair-mm",
        base_half_spread: float = 0.02,
    ) -> None:
        self.agent_id = agent_id
        self.base_half_spread = base_half_spread

    def propose(
        self,
        *,
        reference_fv: dict[str, Any],
        venue_view: dict[str, Any],
        inventory: dict[str, Any],
        params: dict[str, Any],
        clock: int,
    ) -> TargetQuoteSet:
        fv = reference_fv.get("fv")
        if fv is None:
            return TargetQuoteSet(
                fixture_id=0, tick_seq=0, ts=clock, quotes=[], regime="NO_QUOTE"
            )

        # Build the gate context from the injected snapshot. Reference-feed
        # signals come from `reference_fv`; policy thresholds from `params`;
        # inventory from `inventory.get("net", ...)` (never subscript, so the
        # existing `inventory={}` cases still classify as MAKER_SAFE).
        inventory_net = inventory.get("net", 0.0)
        ctx = GateContext(
            suspended=reference_fv.get("suspended", False),
            staleness_s=reference_fv.get("staleness_s", 0),
            freshness_s=params.get("freshness_s", 120),
            event_cooldown_active=reference_fv.get("event_cooldown_active", False),
            fair_vol_bps=reference_fv.get("fair_vol_bps", 0),
            fair_vol_widen_bps=params.get("fair_vol_widen_bps", 300),
            inventory=inventory_net,
            inventory_extreme=params.get("inventory_extreme", 0.5),
        )
        state = classify_state(ctx)

        # A gate (NO_QUOTE / ONE_SIDED_REDUCE) always wins over A-S shaping:
        # `as_shaping` is applied ONLY inside MAKER_SAFE / WIDEN and can NEVER
        # manufacture quotes when the state is a gate (CON-009 / AC-008).
        if state is MakerState.NO_QUOTE:
            return TargetQuoteSet(
                fixture_id=0, tick_seq=0, ts=clock, quotes=[], regime=MakerState.NO_QUOTE.value
            )

        widen_multiplier = params.get("widen_multiplier", 1.0)
        if state is MakerState.WIDEN:
            half_spread = self.base_half_spread * widen_multiplier
        else:
            half_spread = self.base_half_spread

        bid = TargetQuote(
            side=Side.BID,
            market_key=_MARKET_KEY,
            price=round(fv - half_spread, 4),
            size=1.0,
        )
        ask = TargetQuote(
            side=Side.ASK,
            market_key=_MARKET_KEY,
            price=round(fv + half_spread, 4),
            size=1.0,
        )

        if state is MakerState.ONE_SIDED_REDUCE:
            # Quote only the inventory-reducing side: long (net > 0) reduces by
            # asking; short (net < 0) reduces by bidding.
            quotes = [ask] if inventory_net > 0 else [bid]
        else:
            # MAKER_SAFE / WIDEN: two-sided. A-S shaping (if present) may adjust
            # these quotes here, but never bypasses a gate.
            quotes = [bid, ask]

        return TargetQuoteSet(
            fixture_id=0, tick_seq=0, ts=clock, quotes=quotes, regime=state.value
        )

    def params_hash_inputs(self) -> str:
        return f"txline_fair:hs={self.base_half_spread}"
