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
        if fv is None or reference_fv.get("suspended"):
            return TargetQuoteSet(
                fixture_id=0, tick_seq=0, ts=clock, quotes=[], regime="NO_QUOTE"
            )

        bid = TargetQuote(
            side=Side.BID,
            market_key=_MARKET_KEY,
            price=round(fv - self.base_half_spread, 4),
            size=1.0,
        )
        ask = TargetQuote(
            side=Side.ASK,
            market_key=_MARKET_KEY,
            price=round(fv + self.base_half_spread, 4),
            size=1.0,
        )
        return TargetQuoteSet(fixture_id=0, tick_seq=0, ts=clock, quotes=[bid, ask])

    def params_hash_inputs(self) -> str:
        return f"txline_fair:hs={self.base_half_spread}"
