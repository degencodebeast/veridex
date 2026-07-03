"""Market-quality eligibility gate — Phase 2D Post-2D Task 5-6 (S2).

Eligibility, NOT proof scoring: a market-quality filter decides whether a market is worth
strategizing on (degenerate near-certain lines, thin ticks, short horizon, unmapped, or a
close that never priced) — it never touches the deterministic-law score. CON-S2-005: this
module MUST NOT import from ``veridex.law`` or ``veridex.scoring``, and MUST NOT mutate any
score. It only returns a :class:`MarketQualityResult`.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel

from veridex.runtime.evidence import serialize_payload


class MarketQualityConfig(BaseModel):
    """Thresholds an eligibility filter run is judged against."""

    band_lo: float
    band_hi: float
    min_tick_count: int
    min_horizon_s: int

    def filter_config_hash(self) -> str:
        """SHA-256 over the canonically-serialized config (stable, order-independent)."""
        return hashlib.sha256(serialize_payload(self.model_dump()).encode()).hexdigest()


DEFAULT_MARKET_QUALITY_CONFIG = MarketQualityConfig(
    band_lo=0.05, band_hi=0.95, min_tick_count=30, min_horizon_s=600
)
