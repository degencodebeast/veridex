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
    "ALLOWED_CROSS_RULES",
    "ALLOWED_EX_ANTE_FIELDS",
    "ALLOWED_FILL_RULES",
    "ALLOWED_PARTIAL_FILL_POLICIES",
    "ALLOWED_RULE_PARAM_KEYS",
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

# CON-106/CON-107 (Gate-#3 Round-2 Major): ex-ante honesty must cover the ENTIRE
# fill-mechanism surface, not just ``fill_probability_rule`` + ``ex_ante_fields``.
# ``cross_rule``, ``partial_fill_policy`` and ``rule_params`` ALSO declare fill
# mechanics and flow into ``config_hash`` as "pinned"; left unvalidated they let a
# config declare non-ex-ante / tape-reactive mechanics (e.g. a ``trade_crossed``
# cross, a ``post_hoc_fill`` policy, a future-looking ``rule_params`` key) that
# would still hash + construct as honest R2. So each is constrained POSITIVELY by
# an allowlist (CON-106), backstopped by the ``FORBIDDEN_R2_TRIGGERS`` denylist.

# ALLOWED_CROSS_RULES: the finite set of ``cross_rule`` values the R2 lane
# supports. On mids-only data there is no tape and no depth, so the ONLY honest
# reference a quote can cross against is the mid; every value constructed by the
# render/tests is ``"mid"``. A tape-reactive cross (``"trade_crossed"``) is
# rejected by both this allowlist and the forbidden-trigger denylist.
ALLOWED_CROSS_RULES = frozenset({"mid"})

# ALLOWED_PARTIAL_FILL_POLICIES: the finite set of ``partial_fill_policy`` values
# the R2 lane supports. Without depth, partial fills can never be modeled
# honestly, so the only supported policy is ``"none"`` (the value every
# render/test path uses). A post-hoc policy (``"post_hoc_fill"``) is rejected.
ALLOWED_PARTIAL_FILL_POLICIES = frozenset({"none"})

# ALLOWED_RULE_PARAM_KEYS: the supported ``rule_params`` key set per
# ``fill_probability_rule``. ``render_r2_suite`` reads ONLY ``rule_params["p"]``
# (``_pinned_fill_probability`` in ``r2_suite.py``), so for both allowlisted
# static rules the sole supported key is ``"p"``. Any extra key is rejected: it
# is not read by the render yet is PUBLISHED into
# ``assumption_sensitivity["rule_params"]``, so an unread future-looking key
# (``"future_score_after_quote"``) or a forbidden trigger split across keys
# (``{"trade", "crossed"}``) would otherwise pass as pinned/honest.
ALLOWED_RULE_PARAM_KEYS: dict[str, frozenset[str]] = {
    "static_fill_prob": frozenset({"p"}),
    "static_touch_prob": frozenset({"p"}),
}


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

        Gate-#3 Round-2 Major (CON-106/CON-107): the SAME allowlist-first +
        denylist-backstop discipline is applied to the ENTIRE fill-mechanism
        surface, not just ``fill_probability_rule``/``ex_ante_fields`` --
        ``cross_rule`` (allowlist ``ALLOWED_CROSS_RULES``), ``partial_fill_policy``
        (allowlist ``ALLOWED_PARTIAL_FILL_POLICIES``), ``rule_params`` (keys
        constrained per rule via ``ALLOWED_RULE_PARAM_KEYS``) and ``fill_model_id``
        (free-form, denylist-scanned) all declare fill mechanics and flow into
        ``config_hash``, so all are validated here in one place.
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

        # --- CON-106/CON-107 (Gate-#3 Round-2 Major): the REST of the
        # fill-mechanism surface. cross_rule / partial_fill_policy / rule_params /
        # fill_model_id also DECLARE fill mechanics and flow into config_hash as
        # "pinned"; each is constrained here so no third gap remains. ---

        # rule_params: constrain keys to the supported set for the selected rule
        # (render reads only rule_params["p"]). An extra key is not computed yet
        # is PUBLISHED into assumption_sensitivity, so reject it. This also
        # catches a forbidden trigger split across keys ({"trade","crossed"}),
        # since neither key is in the allowed set.
        allowed_param_keys = ALLOWED_RULE_PARAM_KEYS.get(
            self.fill_probability_rule, frozenset({"p"})
        )
        extra_keys = set(self.rule_params) - allowed_param_keys
        if extra_keys:
            raise ValueError(
                f"rule_params keys {sorted(extra_keys)} are not supported by "
                f"fill_probability_rule {self.fill_probability_rule!r}; the render "
                f"computes only {sorted(allowed_param_keys)}. An unread key still "
                f"hashes + publishes as pinned, so it is rejected."
            )
        # Defense-in-depth: denylist-scan rule_params keys AND any string values
        # for a forbidden tape-reactive trigger (catches a future-looking or
        # tape-reactive key/value even if it were ever admitted to the key set).
        for key, value in self.rule_params.items():
            lowered_key = key.lower()
            if any(trigger in lowered_key for trigger in FORBIDDEN_R2_TRIGGERS):
                raise ValueError(
                    f"rule_params key {key!r} embeds a tape-reactive trigger; R2 "
                    f"fill mechanics must be ex-ante only. Forbidden: "
                    f"{sorted(FORBIDDEN_R2_TRIGGERS)}."
                )
            if isinstance(value, str) and any(
                trigger in value.lower() for trigger in FORBIDDEN_R2_TRIGGERS
            ):
                raise ValueError(
                    f"rule_params[{key!r}] value {value!r} embeds a tape-reactive "
                    f"trigger; R2 fill mechanics must be ex-ante only. Forbidden: "
                    f"{sorted(FORBIDDEN_R2_TRIGGERS)}."
                )

        # cross_rule: ALLOWLIST (positive ex-ante proof) + forbidden-trigger
        # denylist. Declares HOW a quote crosses into a fill; a tape-reactive
        # cross (trade_crossed) is barred.
        cross = self.cross_rule.lower()
        if any(trigger in cross for trigger in FORBIDDEN_R2_TRIGGERS):
            raise ValueError(
                f"cross_rule {self.cross_rule!r} names a tape-reactive trigger; R2 "
                f"fill mechanics must be ex-ante only. Forbidden: "
                f"{sorted(FORBIDDEN_R2_TRIGGERS)}."
            )
        if self.cross_rule not in ALLOWED_CROSS_RULES:
            raise ValueError(
                f"cross_rule {self.cross_rule!r} is not an allowlisted ex-ante "
                f"cross; on mids-only data the only honest cross is the mid. "
                f"Allowed: {sorted(ALLOWED_CROSS_RULES)}."
            )

        # partial_fill_policy: ALLOWLIST + forbidden-trigger denylist. Declares
        # fill mechanics; a post-hoc policy (post_hoc_fill) is barred.
        policy = self.partial_fill_policy.lower()
        if any(trigger in policy for trigger in FORBIDDEN_R2_TRIGGERS):
            raise ValueError(
                f"partial_fill_policy {self.partial_fill_policy!r} names a "
                f"tape-reactive trigger; R2 fill mechanics must be ex-ante only. "
                f"Forbidden: {sorted(FORBIDDEN_R2_TRIGGERS)}."
            )
        if self.partial_fill_policy not in ALLOWED_PARTIAL_FILL_POLICIES:
            raise ValueError(
                f"partial_fill_policy {self.partial_fill_policy!r} is not an "
                f"allowlisted policy; without depth, partial fills cannot be "
                f"modeled honestly. Allowed: {sorted(ALLOWED_PARTIAL_FILL_POLICIES)}."
            )

        # fill_model_id: free-form ID (a full allowlist is too strict), so scan
        # it with the forbidden-trigger denylist — it must not NAME a forbidden
        # tape-reactive trigger.
        if any(
            trigger in self.fill_model_id.lower() for trigger in FORBIDDEN_R2_TRIGGERS
        ):
            raise ValueError(
                f"fill_model_id {self.fill_model_id!r} names a tape-reactive "
                f"trigger; an R2 fill model must not identify itself by a "
                f"post-hoc/tape-reactive mechanic. Forbidden: "
                f"{sorted(FORBIDDEN_R2_TRIGGERS)}."
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
