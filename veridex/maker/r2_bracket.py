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
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
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
    draw_mode: str = "DETERMINISTIC_EXPECTED"
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
        """R2 fill rule is ex-ante only (CON-107, AC-116).

        The fill rule and every declared ``ex_ante_fields`` entry must use only
        information available AT QUOTE TIME. Naming any tape-reactive trigger
        turns the overlay into a post-hoc fill claim, which is rejected here.
        """
        # Case-insensitive SUBSTRING matching so neither a cased spelling
        # (``TRADE_CROSSED``) nor a decorated field (``trade_crossed_signal``)
        # can slip a forbidden trigger past the guard (CON-107, AC-116).
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
