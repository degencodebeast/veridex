"""Pure-tier ``StrategyConfig`` + canonical ``config_hash`` (REQ-040/041/042/043, RED-04/32).

The frozen strategy config is the strategy's IDENTITY surface: every behavior knob is typed,
range-checked, immutable, ``extra="forbid"``, and hash-bound. ``config_hash()`` is the single
byte authority for that identity — ``sha256`` over the SHARED
``veridex.runtime.evidence.serialize_payload`` of ``model_dump()`` (no ad-hoc/mirror serializer,
so the same content yields the same hash in every process; REQ-040, RED-32 hash channel).

The tests below pin, all against the REAL config (no re-implementation):
- ``test_every_knob_moves_config_hash`` — each perturbable field, when changed, moves the hash
  (no unbound/ignored knob; REQ-041). The parametrization is kept EXHAUSTIVE via a completeness
  guard so a newly-added field cannot silently escape the hash channel.
- ``test_guard_enabled_has_no_default`` — ``guard_enabled`` is REQUIRED with NO default
  (constructing without it raises); guarded-vs-unguarded is never an implicit choice.
- ``test_incompatible_config_rejected`` — the cross-field invariants (``extreme_multiple>1``;
  ``boundary_zone ⊂ (0,1)``; ``two_sided_band ⊂ boundary_zone``) reject nonsensical configs
  (REQ-042/043).
- ``test_hash_uses_serialize_payload_bytes`` — ``config_hash()`` equals the hash of
  ``serialize_payload(model_dump())`` byte-for-byte (proves the shared serializer, not an
  ad-hoc one; RED-32).
- ``test_config_model_fields_exact_image`` — ``frozenset(model_fields)`` equals a pinned literal
  of the §9.1 field names EXACTLY, proving NO dormant ``microprice``/``smoothed_anchor``/
  ``logit_residual``/``quote_age``/``convergence``/``reach`` feature flag can hide in the config
  even though ``extra="forbid"`` blocks only unknown INPUT keys, not declared fields (REQ-051).
"""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from veridex.mm_strategy.config import StrategyConfig
from veridex.runtime.evidence import serialize_payload

# The EXACT §9.1 field image, pinned as a literal (REQ-051). This is intentionally spelled out
# rather than derived from the class: adding a dormant field to ``StrategyConfig`` (e.g. a
# ``microprice_weight`` flag) must break ``test_config_model_fields_exact_image`` here.
_EXPECTED_FIELDS = frozenset(
    {
        # pinned strategy identity (REQ-044 field; the value is pinned in contracts by E1-T5)
        "strategy_id",
        # basis / residual estimator knobs
        "residual_band",
        "extreme_multiple",
        "basis_estimator",
        "basis_window",
        "ewma_halflife_ms",
        "basis_min_samples",
        # freshness / skew gates
        "fv_freshness_ms",
        "fv_source_lag_s",
        "book_freshness_ms",
        "max_leg_skew_ms",
        # quoting geometry
        "half_spread",
        "boundary_zone",
        "two_sided_band",
        # book-health floors
        "min_top_depth",
        "min_level_count",
        "depth_collapse_ratio",
        "ref_min_samples",
        # event-protection knobs
        "book_state_dwell_before_quote_ms",
        "mid_jump_threshold",
        "spread_blowout_multiple",
        # inventory / rounding
        "inventory_soft_limit",
        "price_epsilon",
        # smoother + rolling references
        "event_smoother",
        "event_smoother_param",
        "market_status_max_age_ms",
        "rolling_spread_window",
        "rolling_depth_window",
        # lifecycle / execution policy
        "guard_enabled",
        "restart_policy",
        "tif",
        "anchor_mode",
        # fee model
        "fee_bps",
    }
)

# ``anchor_mode`` is a MONO-VALUED literal by design (only ``"mid"``; REQ-051 forbids a
# microprice/smoothed anchor) — it has no alternate valid value, so it cannot be perturbed and is
# deliberately excluded from the "moves the hash" parametrization (its presence is proven by the
# exact-image test + the dedicated mono-value assertion below).
_MONO_VALUED_FIELDS = frozenset({"anchor_mode"})

# For every PERTURBABLE field: a valid alternate value that (a) differs from the default and
# (b) still satisfies all single- and cross-field validators. Reconstructing the whole config with
# one field swapped (rather than ``model_copy``) re-runs validation, so each alternate is proven
# constructible. ``guard_enabled`` starts ``True`` in the base config so its alternate ``False``
# differs.
_PERTURBATIONS: dict[str, object] = {
    "strategy_id": "some-other-strategy",
    "residual_band": 0.03,
    "extreme_multiple": 4.0,
    "basis_estimator": "halflife_ewma",
    "basis_window": 601,
    "ewma_halflife_ms": 300_001,
    "basis_min_samples": 31,
    "fv_freshness_ms": 10_001,
    "fv_source_lag_s": 11,
    "book_freshness_ms": 5_001,
    "max_leg_skew_ms": 2_001,
    "half_spread": 0.03,
    "boundary_zone": (0.05, 0.95),
    "two_sided_band": (0.31, 0.69),
    "min_top_depth": 51.0,
    "min_level_count": 4,
    "depth_collapse_ratio": 0.26,
    "ref_min_samples": 21,
    "book_state_dwell_before_quote_ms": 5_001,
    "mid_jump_threshold": 0.03,
    "spread_blowout_multiple": 4.0,
    "inventory_soft_limit": 0.6,
    "price_epsilon": 0.006,
    "event_smoother": "halflife_ewma",
    "event_smoother_param": 0.2,
    "market_status_max_age_ms": 30_001,
    "rolling_spread_window": 121,
    "rolling_depth_window": 121,
    "guard_enabled": False,
    "restart_policy": "fail_open",
    "tif": "IOC",
    "fee_bps": 101.0,
}


def _base_config() -> StrategyConfig:
    """A fully-defaulted config with the ONE required knob (``guard_enabled``) supplied."""
    return StrategyConfig(guard_enabled=True)


def test_perturbation_map_is_exhaustive() -> None:
    # Completeness guard (teeth): the perturbation map plus the mono-valued fields must cover EVERY
    # declared field. A newly-added perturbable knob with no mapping fails HERE, so it can never
    # skip the "moves the hash" coverage below.
    covered = frozenset(_PERTURBATIONS) | _MONO_VALUED_FIELDS
    assert covered == frozenset(StrategyConfig.model_fields), (
        "every StrategyConfig field must be either perturbed or explicitly mono-valued; "
        f"uncovered={frozenset(StrategyConfig.model_fields) - covered}, "
        f"unknown={covered - frozenset(StrategyConfig.model_fields)}"
    )


@pytest.mark.parametrize("field", sorted(_PERTURBATIONS))
def test_every_knob_moves_config_hash(field: str) -> None:
    # REQ-041: no unbound/ignored knob. Changing ANY single behavior field changes the identity
    # hash — the field genuinely participates in ``config_hash()``.
    base = _base_config()
    perturbed = StrategyConfig(**{**base.model_dump(), field: _PERTURBATIONS[field]})
    assert perturbed.config_hash() != base.config_hash(), (
        f"perturbing {field!r} did not move config_hash — the knob is not hash-bound"
    )


def test_anchor_mode_is_mono_valued_mid() -> None:
    # REQ-051: the ONLY anchor is the venue mid — there is no microprice/smoothed-anchor mode.
    assert _base_config().anchor_mode == "mid"
    with pytest.raises(ValidationError):
        StrategyConfig(guard_enabled=True, anchor_mode="microprice")


def test_guard_enabled_has_no_default() -> None:
    # REQ: guarded-vs-unguarded must be an EXPLICIT choice — constructing without it raises, and
    # the field is required in the model schema.
    with pytest.raises(ValidationError):
        StrategyConfig()  # type: ignore[call-arg]
    assert StrategyConfig.model_fields["guard_enabled"].is_required()
    # both explicit values construct
    assert StrategyConfig(guard_enabled=True).guard_enabled is True
    assert StrategyConfig(guard_enabled=False).guard_enabled is False


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"extreme_multiple": 1.0}, id="extreme_multiple_not_gt_1"),
        pytest.param({"extreme_multiple": 0.5}, id="extreme_multiple_below_1"),
        pytest.param({"boundary_zone": (0.0, 0.96)}, id="boundary_lo_not_gt_0"),
        pytest.param({"boundary_zone": (0.04, 1.0)}, id="boundary_hi_not_lt_1"),
        pytest.param({"boundary_zone": (0.96, 0.04)}, id="boundary_inverted"),
        pytest.param(
            {"two_sided_band": (0.01, 0.70)}, id="two_sided_below_boundary"
        ),
        pytest.param(
            {"two_sided_band": (0.30, 0.99)}, id="two_sided_above_boundary"
        ),
        pytest.param({"two_sided_band": (0.70, 0.30)}, id="two_sided_inverted"),
    ],
)
def test_incompatible_config_rejected(overrides: dict[str, object]) -> None:
    # REQ-042/043 cross-field invariants: extreme_multiple>1; boundary_zone ⊂ (0,1);
    # two_sided_band ⊂ boundary_zone. Each nonsensical config is rejected at construction.
    with pytest.raises(ValidationError):
        StrategyConfig(guard_enabled=True, **overrides)


def test_hash_uses_serialize_payload_bytes() -> None:
    # RED-32 hash channel: config_hash() is EXACTLY sha256 over the shared serialize_payload of
    # model_dump() — not an ad-hoc/mirror serializer. If the impl swapped to ``str(model_dump())``
    # (or any other serializer), this equality breaks.
    config = _base_config()
    expected = hashlib.sha256(
        serialize_payload(config.model_dump()).encode("utf-8")
    ).hexdigest()
    assert config.config_hash() == expected


def test_config_model_fields_exact_image() -> None:
    # REQ-051: the field image is EXACTLY the §9.1 set — no dormant microprice/smoothed-anchor/
    # logit-residual/quote-age/convergence/reach flag hiding behind ``extra="forbid"`` (which
    # blocks only unknown INPUT keys, never a declared-but-forbidden field).
    assert frozenset(StrategyConfig.model_fields) == _EXPECTED_FIELDS


def test_config_is_frozen_and_forbids_extra() -> None:
    # REQ-040: immutable identity + no silent extra knobs at construction.
    config = _base_config()
    with pytest.raises(ValidationError):
        StrategyConfig(guard_enabled=True, microprice_weight=0.5)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        config.residual_band = 0.99  # type: ignore[misc]
