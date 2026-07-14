"""Pure-tier strategy config + canonical ``config_hash`` (REQ-040/041/042/043).

``StrategyConfig`` is the strategy's IDENTITY surface: EVERY behavior knob from spec §9.1 is a
typed, range-checked, immutable, ``extra="forbid"`` field, and ``config_hash()`` binds all of them
into one deterministic identity byte string. ``guard_enabled`` is the sole REQUIRED knob (NO
default) so guarded-vs-unguarded is always an explicit choice, never an implicit one.

``config_hash()`` is the single byte authority: ``sha256`` over the SHARED canonical serializer
``veridex.runtime.evidence.serialize_payload`` of ``model_dump()`` — the SAME serializer every
other evidence hash uses, so identity hashes never diverge across producers/processes (REQ-040).
There is deliberately NO local/mirror serializer here (a mirror could drift; RED-32).

Import whitelist (load-bearing, enforced by ``tests/test_mm_strategy_purity.py``): stdlib +
pydantic + ``veridex.runtime.evidence`` ONLY. This module imports NO sibling
(``contracts``/``basis``/``core``): the config is a leaf on the pure tier.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from veridex.runtime.evidence import serialize_payload


class StrategyConfig(BaseModel):
    """Frozen strategy configuration — every §9.1 behavior knob, immutable and hash-bound.

    Unknown input keys are rejected (``extra="forbid"``) and instances are immutable
    (``frozen=True``). ``config_hash()`` is the canonical identity of the whole knob set.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- pinned strategy identity (REQ-044 field; value pinned in contracts by E1-T5) --------
    strategy_id: str = "venue-anchored-txline-guarded-maker"

    # --- basis / residual estimator (§9.1) ---------------------------------------------------
    residual_band: float = Field(default=0.02, gt=0.0)
    extreme_multiple: float = Field(default=3.0, gt=1.0)
    basis_estimator: Literal["rolling_median", "halflife_ewma"] = "rolling_median"
    basis_window: int = Field(default=600, ge=1)
    ewma_halflife_ms: int = Field(default=300_000, ge=1)
    basis_min_samples: int = Field(default=30, ge=1)

    # --- freshness / skew gates (§9.1) -------------------------------------------------------
    fv_freshness_ms: int = Field(default=10_000, ge=1)
    fv_source_lag_s: int = Field(default=10, ge=0)
    book_freshness_ms: int = Field(default=5_000, ge=1)
    max_leg_skew_ms: int = Field(default=2_000, ge=0)

    # --- quoting geometry (§9.1) -------------------------------------------------------------
    half_spread: float = Field(default=0.02, gt=0.0, lt=1.0)
    boundary_zone: tuple[float, float] = (0.04, 0.96)
    two_sided_band: tuple[float, float] = (0.30, 0.70)

    # --- book-health floors (§9.1) -----------------------------------------------------------
    min_top_depth: float = Field(default=50.0, gt=0.0)
    min_level_count: int = Field(default=3, ge=1)
    depth_collapse_ratio: float = Field(default=0.25, gt=0.0, le=1.0)
    ref_min_samples: int = Field(default=20, ge=1)

    # --- event-protection knobs (§9.1) -------------------------------------------------------
    book_state_dwell_before_quote_ms: int = Field(default=5_000, ge=0)
    mid_jump_threshold: float = Field(default=0.02, gt=0.0)
    spread_blowout_multiple: float = Field(default=3.0, gt=1.0)

    # --- inventory / rounding (§9.1) ---------------------------------------------------------
    inventory_soft_limit: float = Field(default=0.5, gt=0.0)
    price_epsilon: float = Field(default=0.005, gt=0.0)

    # --- smoother + rolling references (§9.1) ------------------------------------------------
    event_smoother: Literal["ema_alpha", "halflife_ewma"] = "ema_alpha"
    event_smoother_param: float = Field(default=0.1, gt=0.0, le=1.0)
    market_status_max_age_ms: int = Field(default=30_000, ge=1)
    rolling_spread_window: int = Field(default=120, ge=1)
    rolling_depth_window: int = Field(default=120, ge=1)

    # --- lifecycle / execution policy (§9.1) -------------------------------------------------
    # REQUIRED, NO default: guarded-vs-unguarded is always an explicit choice.
    guard_enabled: bool
    restart_policy: Literal["fail_closed", "fail_open"] = "fail_closed"
    tif: Literal["GTC", "GTD", "IOC", "FOK"] = "GTC"
    # Mono-valued by design: the ONLY anchor is the venue mid (REQ-051 — no microprice/smoothed).
    anchor_mode: Literal["mid"] = "mid"

    # --- fee model (§9.1) --------------------------------------------------------------------
    # The pinned symmetric fee rate for the ``bps · min(p, 1-p) · size`` model; carried in the
    # config so fees are NEVER assumed zero. Non-negative (a genuinely fee-free market is 0).
    fee_bps: float = Field(default=100.0, ge=0.0)

    @model_validator(mode="after")
    def _validate_zones(self) -> StrategyConfig:
        """Cross-field zone invariants (REQ-042/043).

        - ``boundary_zone`` is a strictly-ordered sub-interval of the open unit interval ``(0, 1)``.
        - ``two_sided_band`` is a strictly-ordered sub-interval CONTAINED IN ``boundary_zone``
          (two-sided liquidity is only ever offered strictly inside the quoting boundary).
        """
        b_lo, b_hi = self.boundary_zone
        if not (0.0 < b_lo < b_hi < 1.0):
            raise ValueError(
                f"boundary_zone must satisfy 0 < lo < hi < 1, got {self.boundary_zone!r}"
            )
        t_lo, t_hi = self.two_sided_band
        if not (t_lo < t_hi):
            raise ValueError(
                f"two_sided_band must satisfy lo < hi, got {self.two_sided_band!r}"
            )
        if not (b_lo <= t_lo and t_hi <= b_hi):
            raise ValueError(
                f"two_sided_band {self.two_sided_band!r} must be contained in "
                f"boundary_zone {self.boundary_zone!r}"
            )
        return self

    def config_hash(self) -> str:
        """``sha256`` hexdigest over ``serialize_payload(model_dump())`` (canonical identity).

        Uses the SHARED ``veridex.runtime.evidence.serialize_payload`` (sorted keys, compact
        separators) directly — no local/mirror serializer — so the identity hash is byte-stable
        across every process and matches every other evidence hash's canonicalization (REQ-040).
        """
        canonical = serialize_payload(self.model_dump())
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
