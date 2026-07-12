"""Maker state machine.

Determines the maker's regime each tick. NO_QUOTE has the HIGHEST
precedence: it must override widen/inventory conditions unconditionally,
because quoting through a goal/suspension/stale feed is how a maker gets
picked off (CON-009).
"""

from enum import Enum

from pydantic import BaseModel


class MakerState(str, Enum):
    """Maker regime for the current tick."""

    MAKER_SAFE = "MAKER_SAFE"
    WIDEN = "WIDEN"
    ONE_SIDED_REDUCE = "ONE_SIDED_REDUCE"
    NO_QUOTE = "NO_QUOTE"


class GateContext(BaseModel):
    """Inputs required to classify the maker state for a tick."""

    suspended: bool
    staleness_s: int
    freshness_s: int
    event_cooldown_active: bool
    fair_vol_bps: int
    fair_vol_widen_bps: int
    inventory: float
    inventory_extreme: float


def classify_state(ctx: GateContext) -> MakerState:
    """Classify the maker state from ``ctx``.

    Precedence (highest first):
      1. NO_QUOTE   - suspended, stale feed, or event cooldown (unconditional)
      2. ONE_SIDED_REDUCE - inventory at/over the extreme threshold
      3. WIDEN      - fair volatility at/over the widen threshold
      4. MAKER_SAFE - otherwise
    """
    if ctx.suspended or ctx.staleness_s > ctx.freshness_s or ctx.event_cooldown_active:
        return MakerState.NO_QUOTE
    elif abs(ctx.inventory) >= ctx.inventory_extreme:
        return MakerState.ONE_SIDED_REDUCE
    elif ctx.fair_vol_bps >= ctx.fair_vol_widen_bps:
        return MakerState.WIDEN
    else:
        return MakerState.MAKER_SAFE
