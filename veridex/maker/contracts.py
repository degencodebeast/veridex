"""Proposal-only contracts for market-maker agents.

These are intentionally a distinct contract from the directional agent loop's
decision contracts: a `MarketMakerAgent` only *proposes* a `TargetQuoteSet` (a
target quote ladder to reconcile toward), it never issues orders directly. This
keeps the maker loop out of the directional decision path.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class Side(Enum):
    BID = "BID"
    ASK = "ASK"


class TargetQuote(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    side: Side
    market_key: str
    price: float
    size: float
    post_only: bool = True
    reason: str = "quote"


class TargetQuoteSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fixture_id: int
    tick_seq: int
    ts: int
    quotes: list[TargetQuote]
    regime: str = "QUIET"
    inventory_snapshot: dict[str, float] = Field(default_factory=dict)


class MakerRungLabel(str, Enum):
    R1 = "MM-R1"
    R1_5 = "MM-R1.5"
    R2 = "MM-R2"
    R3 = "MM-R3"
    R4 = "MM-R4"


class MarketMakerAgent(Protocol):
    agent_id: str

    def propose(
        self,
        *,
        reference_fv: dict[str, float],
        venue_view: dict[str, float],
        inventory: dict[str, float],
        params: dict[str, Any],
        clock: int,
    ) -> TargetQuoteSet: ...
