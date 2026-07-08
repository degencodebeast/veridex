"""MM-R2 report-only fill-assumption sensitivity bracket.

R2 is a DECLARED MODEL OVERLAY, never a measurement. On mids-only data there is
no depth and no cancels, so any "fill" is an ASSUMPTION typed into a pinned
config. Results are labeled ``UNCALIBRATED`` and are structurally barred from
any ranking. Because there is no depth, queue position can never be modeled at
R2 -> ``queue_modeled`` MUST be ``False`` (``True`` is rejected at construction).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "ALLOWED_EX_ANTE_FIELDS",
    "ALLOWED_FILL_RULES",
    "FORBIDDEN_R2_TRIGGERS",
    "FillAssumptionConfig",
    "render_sensitivity_bracket",
]

_BRACKET_LABEL = "UNCALIBRATED / declared model overlay"

# Tape-reactive triggers that peek at data NOT available at quote time. An R2
# fill rule (or any declared ex-ante field) naming one of these would turn the
# overlay into a post-hoc fill claim, which R2 must never make (CON-107).
FORBIDDEN_R2_TRIGGERS = frozenset(
    {"trade_crossed", "price_touched", "mid_crossed", "fv_moved", "post_hoc_fill"}
)

# CON-106 (Gate-#3 Major 1): ex-ante honesty is proven POSITIVELY by an
# ALLOWLIST, not by an open-ended denylist. A denylist only rejects the tokens
# it happens to know; a rule/field that references a LATER observation while
# avoiding those tokens (e.g. "uses_future_score_after_quote",
# "future_score_after_quote", "realized_edge_next_bar") would slip through. The
# allowlist inverts this: only the finite set of rules the render can actually
# compute and the finite set of quote-time-only inputs are accepted; everything
# else — including any novel future-referencing name — is rejected by default.

# ALLOWED_FILL_RULES: the finite set of ``fill_probability_rule`` IDs that
# ``render_r2_suite`` can actually COMPUTE. The render dispatches a SINGLE static
# fill-probability mechanism (``_pinned_fill_probability`` -> ``rule_params["p"]``);
# both supported IDs name that same static ex-ante rule. Derived from the render
# logic in ``r2_suite.py`` + the passing R2 tests — no other rule ID is computed
# anywhere in the maker lane.
ALLOWED_FILL_RULES = frozenset({"static_fill_prob", "static_touch_prob"})

# ALLOWED_EX_ANTE_FIELDS: the finite set of quote-time-only field names a rule may
# reference. Spec §4.4 declares ``ex_ante_fields`` as "allowed at-quote-time
# inputs" but does NOT enumerate them, so this set is DERIVED from the quote-time
# quantities the render/rules legitimately use: every entry is knowable AT QUOTE
# TIME and NONE references any later / realized / future observation (that is the
# CON-106 ex-ante boundary). Seeded by the fields the passing R2 tests declare
# (``quote_price``, ``quoted_half_spread``) plus the other quote-time primitives
# (reference price, quoted spread/size, latency, near-band).
ALLOWED_EX_ANTE_FIELDS = frozenset(
    {
        "quote_price",
        "quote_size",
        "quoted_half_spread",
        "half_spread",
        "ref_now",
        "ref_price",
        "latency_ms",
        "near_band",
    }
)


def _canonical_dump(obj: Any) -> str:
    """Deterministic, compact JSON serialization for hashing.

    Inlined here on purpose: R2 must not import evidence-layer helpers.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


class FillAssumptionConfig(BaseModel):
    """Pinned, frozen fill-assumption config for an R2 sensitivity bracket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fill_model_id: str
    latency_ms: int
    cross_rule: str
    partial_fill_policy: str
    queue_modeled: bool = False
    notes: str = ""
    # --- E6: pinned ex-ante fill rule (all flow into config_hash) ---
    fill_probability_rule: str = "static_fill_prob"
    rule_params: dict[str, float] = Field(default_factory=dict)
    draw_mode: Literal["SEEDED_STOCHASTIC", "DETERMINISTIC_EXPECTED"] = (
        "DETERMINISTIC_EXPECTED"
    )
    seed: int | None = None
    n_paths: int | None = None
    ex_ante_fields: list[str] = Field(default_factory=list)

    @field_validator("queue_modeled")
    @classmethod
    def _reject_queue_modeled(cls, value: bool) -> bool:
        """Depth is unavailable at R2 -> queue position can never be modeled."""
        if value:
            raise ValueError(
                "queue_modeled must be False at R2: depth is unavailable on "
                "mids-only data, so queue position can never be modeled."
            )
        return value

    @model_validator(mode="after")
    def _reject_tape_reactive_triggers(self) -> "FillAssumptionConfig":
        """R2 fill rule is ex-ante only (CON-106/CON-107, AC-116).

        Ex-ante-ness is proven POSITIVELY by an ALLOWLIST: ``fill_probability_rule``
        must be one the render can actually COMPUTE (``ALLOWED_FILL_RULES``) and
        EVERY declared ``ex_ante_fields`` entry must be a quote-time-only input
        (``ALLOWED_EX_ANTE_FIELDS``). A denylist alone cannot prove this — a name
        that references a later observation while avoiding the known tokens (e.g.
        ``uses_future_score_after_quote``) would slip through — so the allowlist is
        the primary guard. The ``FORBIDDEN_R2_TRIGGERS`` denylist is kept as
        defense-in-depth, giving a clear, specific error for known-bad triggers.
        """
        # Defense-in-depth: case-insensitive SUBSTRING matching gives a specific
        # error for a known tape-reactive trigger, whether cased (``TRADE_CROSSED``)
        # or decorated (``trade_crossed_signal``) (CON-107, AC-116).
        rule = self.fill_probability_rule.lower()
        if any(trigger in rule for trigger in FORBIDDEN_R2_TRIGGERS):
            raise ValueError(
                f"fill_probability_rule {self.fill_probability_rule!r} names a "
                f"tape-reactive trigger; R2 fill rules must be ex-ante only. "
                f"Forbidden: {sorted(FORBIDDEN_R2_TRIGGERS)}."
            )
        for field in self.ex_ante_fields:
            lowered = field.lower()
            if any(trigger in lowered for trigger in FORBIDDEN_R2_TRIGGERS):
                raise ValueError(
                    f"ex_ante_fields entry {field!r} embeds a tape-reactive "
                    f"trigger; R2 may only declare fields available at quote "
                    f"time. Forbidden: {sorted(FORBIDDEN_R2_TRIGGERS)}."
                )

        # Primary ex-ante proof: positive allowlist membership (CON-106).
        if self.fill_probability_rule not in ALLOWED_FILL_RULES:
            raise ValueError(
                f"fill_probability_rule {self.fill_probability_rule!r} is not an "
                f"allowlisted ex-ante rule the render can compute; R2 proves "
                f"ex-ante-ness positively. Allowed: {sorted(ALLOWED_FILL_RULES)}."
            )
        for field in self.ex_ante_fields:
            if field not in ALLOWED_EX_ANTE_FIELDS:
                raise ValueError(
                    f"ex_ante_fields entry {field!r} is not an allowlisted "
                    f"quote-time input; R2 may only declare fields available AT "
                    f"QUOTE TIME. Allowed: {sorted(ALLOWED_EX_ANTE_FIELDS)}."
                )
        return self

    @model_validator(mode="after")
    def _require_pinned_seed_and_paths(self) -> "FillAssumptionConfig":
        """SEEDED_STOCHASTIC must pin ``seed`` and ``n_paths`` (CON-108).

        ``config_hash`` claims the run is pinned/deterministic; a ``seed=None``
        draws from OS entropy (different percentiles each run) and a missing /
        non-positive ``n_paths`` yields a degenerate all-zeros distribution.
        ``DETERMINISTIC_EXPECTED`` is unaffected.
        """
        if self.draw_mode == "SEEDED_STOCHASTIC":
            if self.seed is None:
                raise ValueError(
                    "SEEDED_STOCHASTIC requires a pinned 'seed' (got None): an "
                    "unseeded draw is non-deterministic yet config_hash claims a "
                    "pinned run."
                )
            if not (isinstance(self.n_paths, int) and self.n_paths > 0):
                raise ValueError(
                    f"SEEDED_STOCHASTIC requires a positive int 'n_paths', got "
                    f"{self.n_paths!r}: a missing/non-positive n_paths yields a "
                    f"degenerate empty distribution."
                )
        return self

    def config_hash(self) -> str:
        """Stable content hash of the pinned config."""
        return hashlib.sha256(
            _canonical_dump(self.model_dump()).encode()
        ).hexdigest()


def render_sensitivity_bracket(
    markouts: list[int], cfg: FillAssumptionConfig
) -> dict[str, Any]:
    """Render a report-only pessimistic/neutral/optimistic bracket.

    Args:
        markouts: Markout values under the declared fill assumptions.
        cfg: Pinned fill-assumption config (governs provenance, not ranking).

    Returns:
        A report-only payload. ``label``, ``ranked=False`` and
        ``queue_modeled=False`` are HARDCODED: an R2 bracket is always
        UNCALIBRATED and structurally barred from ranking.

    Raises:
        ValueError: If ``markouts`` is empty.
    """
    if not markouts:
        raise ValueError("'markouts' must be non-empty to render a bracket")

    pessimistic = min(markouts)
    optimistic = max(markouts)
    neutral = round(sum(markouts) / len(markouts))

    return {
        "bracket": {
            "pessimistic": pessimistic,
            "neutral": neutral,
            "optimistic": optimistic,
        },
        "label": _BRACKET_LABEL,
        "queue_modeled": False,
        "ranked": False,
    }
