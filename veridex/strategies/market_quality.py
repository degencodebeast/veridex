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


class MarketQualityResult(BaseModel):
    """Eligibility verdict for one market, with a named reason per failing rule."""

    market_ref: str
    eligible: bool
    reasons: list[str]
    near_certain: bool
    tick_count: int
    horizon_s: int
    mapping_valid: bool
    close_quality: str
    filter_config_hash: str


def evaluate_market_quality(
    *,
    market_ref: str,
    implied_prob: float,
    tick_count: int,
    horizon_s: int,
    mapping_valid: bool,
    close_quality: str,
    config: MarketQualityConfig,
) -> MarketQualityResult:
    """Judge one market against ``config`` and surface every failing rule by name.

    Never hides a failure mode behind a single generic "ineligible" flag — each rule that
    fails appends its own named reason so operators can see exactly why a market was excluded
    (e.g. a suspended close is surfaced as ``"close_suspended"``, not silently dropped).
    """
    near_certain = implied_prob < config.band_lo or implied_prob > config.band_hi
    reasons: list[str] = []
    if near_certain:
        reasons.append("near_certain")
    if tick_count < config.min_tick_count:
        reasons.append("insufficient_ticks")
    if horizon_s < config.min_horizon_s:
        reasons.append("insufficient_horizon")
    if not mapping_valid:
        reasons.append("unmapped")
    if close_quality == "suspended":
        reasons.append("close_suspended")
    elif close_quality != "priced":
        reasons.append("close_missing")  # covers "missing" and any other non-priced value

    return MarketQualityResult(
        market_ref=market_ref,
        eligible=not reasons,
        reasons=reasons,
        near_certain=near_certain,
        tick_count=tick_count,
        horizon_s=horizon_s,
        mapping_valid=mapping_valid,
        close_quality=close_quality,
        filter_config_hash=config.filter_config_hash(),
    )
