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

from pydantic import BaseModel, ConfigDict, field_validator

__all__ = ["FillAssumptionConfig", "render_sensitivity_bracket"]

_BRACKET_LABEL = "UNCALIBRATED / declared model overlay"


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
