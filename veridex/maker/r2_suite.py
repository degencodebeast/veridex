"""MM-R2 full report-only sensitivity suite + protection ablation.

R2 is a **DECLARED MODEL OVERLAY**, never a fill / fill-rate /
spread-capture-as-PnL / realized-PnL / executable-edge claim, and is NEVER
ranked. Every output is produced ONLY from the pinned ex-ante
``FillAssumptionConfig`` (fields available AT QUOTE TIME) plus report-only
markout inputs -- no tape-reactive trigger, no depth, no queue, no own fills.

Every ``R2SensitivityBracket`` / ``R2ProtectionAblation`` is quadruple-labeled
``REPORT_ONLY / UNCALIBRATED / DECLARED_MODEL_OVERLAY / NOT_A_FILL_PROOF`` with
``ranked=False``, ``queue_modeled=False``, ``fill_proof=False``,
``uses_real_orderbook=False``, ``uses_own_fills=False`` and a literal-``None``
``real_executable_edge_bps`` / ``realized_pnl``. ``SEEDED_STOCHASTIC`` returns a
DISTRIBUTION (mean + percentiles) over ``n_paths`` under a pinned ``seed`` -- a
same-seed run is byte-identical, never a cherry-picked single path.
"""

from __future__ import annotations

import math
import random
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from veridex.maker.r2_bracket import FORBIDDEN_R2_TRIGGERS, FillAssumptionConfig

__all__ = [
    "R2_QUADRUPLE_LABEL",
    "FORBIDDEN_TRIGGER_ASSERTION",
    "R2SensitivityScenario",
    "R2SensitivityBracket",
    "R2ProtectionAblation",
    "render_r2_suite",
    "render_protection_ablation",
]

# The mandatory honesty label carried by every R2 overlay artifact.
R2_QUADRUPLE_LABEL = (
    "REPORT_ONLY / UNCALIBRATED / DECLARED_MODEL_OVERLAY / NOT_A_FILL_PROOF"
)

# The pinned no-tape-reactive assertion every bracket declares.
FORBIDDEN_TRIGGER_ASSERTION = (
    "R2 fill rule uses only ex-ante fields available at quote time; no "
    "tape-reactive trigger ("
    + "/".join(sorted(FORBIDDEN_R2_TRIGGERS))
    + ") is used."
)


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile over pre-sorted values (deterministic)."""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * q
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return round(sorted_vals[int(k)], 6)
    return round(sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo), 6)


class R2SensitivityScenario(BaseModel):
    """One pinned-assumption corner of the report-only sensitivity bracket.

    All quantities are ``simulated_``-prefixed model outputs of the pinned
    ex-ante rule -- never observed fills, never realized PnL.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    simulated_expected_inventory: float
    simulated_expected_exposure: float
    simulated_spread_capture_bps: int
    simulated_adverse_selection_haircut_bps: int


class R2SensitivityBracket(BaseModel):
    """Report-only, quadruple-labeled, never-ranked R2 sensitivity overlay.

    Model fields are pinned to EXACTLY the declared-overlay data fields plus the
    honesty guards; no fill / fill-rate / spread-capture-as-PnL / realized-PnL /
    executable-edge value is ever produced (the tombstone fields stay ``None``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- declared-overlay data (all model/simulated provenance) ---
    simulated_expected_inventory_path: dict[str, Any]
    simulated_expected_exposure: dict[str, Any]
    simulated_spread_capture_range: dict[str, Any]
    simulated_adverse_selection_haircut: dict[str, Any]
    assumption_sensitivity: dict[str, Any]
    # --- honesty tombstones (literal None) ---
    realized_pnl: None = None
    real_executable_edge_bps: None = None
    # --- honesty guards (structurally pinned to the honest values) ---
    fill_proof: bool = False
    uses_real_orderbook: bool = False
    uses_own_fills: bool = False
    queue_modeled: bool = False
    ranked: bool = False
    fill_rule_source: str = "pinned_config"
    forbidden_trigger_assertion: str = FORBIDDEN_TRIGGER_ASSERTION
    label: str = R2_QUADRUPLE_LABEL

    @field_validator(
        "fill_proof",
        "uses_real_orderbook",
        "uses_own_fills",
        "queue_modeled",
        "ranked",
    )
    @classmethod
    def _must_be_false(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "R2 overlay guards must be False: it is a declared model "
                "overlay, never a fill proof / real-orderbook / own-fill / "
                "queue / ranked artifact."
            )
        return value

    @field_validator("label")
    @classmethod
    def _must_be_quad_label(cls, value: str) -> str:
        if value != R2_QUADRUPLE_LABEL:
            raise ValueError(
                f"R2 bracket label must be {R2_QUADRUPLE_LABEL!r}, got {value!r}."
            )
        return value


class R2ProtectionAblation(BaseModel):
    """Protection ON/OFF ablation -- two declared overlays, never ranked.

    ``protection_on`` / ``protection_off`` are both ``render_r2_suite`` outputs
    under different pinned assumptions; ``event_gate_cost`` is a MODEL delta, not
    a realized number.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protection_on: R2SensitivityBracket
    protection_off: R2SensitivityBracket
    event_gate_cost: dict[str, Any]
    delta_note: str
    label: str = R2_QUADRUPLE_LABEL

    @field_validator("label")
    @classmethod
    def _must_be_quad_label(cls, value: str) -> str:
        if value != R2_QUADRUPLE_LABEL:
            raise ValueError(
                f"R2 ablation label must be {R2_QUADRUPLE_LABEL!r}, got {value!r}."
            )
        return value


def _pinned_fill_probability(cfg: FillAssumptionConfig) -> float:
    """The ex-ante fill probability pinned in the config (default 0.5)."""
    p = float(cfg.rule_params.get("p", 0.5))
    return min(max(p, 0.0), 1.0)


def _scenario(name: str, markout: float, cfg: FillAssumptionConfig) -> R2SensitivityScenario:
    """A single declared-overlay corner derived purely from cfg + a markout."""
    p = _pinned_fill_probability(cfg)
    inv = round(p * (markout / 100.0), 6)
    return R2SensitivityScenario(
        name=name,
        simulated_expected_inventory=inv,
        simulated_expected_exposure=round(abs(inv), 6),
        simulated_spread_capture_bps=int(round(markout * p)),
        simulated_adverse_selection_haircut_bps=int(round(markout * (1.0 - p))),
    )


def _deterministic_inventory_path(
    markouts: list[int], cfg: FillAssumptionConfig
) -> dict[str, Any]:
    p = _pinned_fill_probability(cfg)
    path: list[float] = []
    inv = 0.0
    for m in markouts:
        inv += p * (m / 100.0)
        path.append(round(inv, 6))
    return {
        "draw_mode": "DETERMINISTIC_EXPECTED",
        "expected_path": path,
        "expected_final": round(inv, 6),
        "note": (
            "expected/model inventory under the pinned ex-ante rule, "
            "NOT shares held and NOT a fill claim"
        ),
    }


def _seeded_inventory_distribution(
    markouts: list[int], cfg: FillAssumptionConfig
) -> dict[str, Any]:
    """Distribution (mean + percentiles) over ``n_paths`` under a pinned seed.

    Never a cherry-picked single path: the reported ``mean_path`` is the
    element-wise mean and the finals are summarized as p10/p50/p90.
    """
    p = _pinned_fill_probability(cfg)
    n_paths = cfg.n_paths or 0
    rng = random.Random(cfg.seed)
    n_steps = len(markouts)
    sums = [0.0] * n_steps
    finals: list[float] = []
    for _ in range(n_paths):
        inv = 0.0
        for i, m in enumerate(markouts):
            filled = 1.0 if rng.random() < p else 0.0
            inv += filled * (m / 100.0)
            sums[i] += inv
        finals.append(inv)
    mean_path = [round(s / n_paths, 6) for s in sums] if n_paths else []
    finals_sorted = sorted(finals)
    return {
        "draw_mode": "SEEDED_STOCHASTIC",
        "seed": cfg.seed,
        "n_paths": n_paths,
        "mean_path": mean_path,
        "mean_final": round(sum(finals) / n_paths, 6) if n_paths else 0.0,
        "p10_final": _percentile(finals_sorted, 0.10),
        "p50_final": _percentile(finals_sorted, 0.50),
        "p90_final": _percentile(finals_sorted, 0.90),
        "note": (
            "distribution over seeded model paths (mean + percentiles), "
            "NOT a single path and NOT shares held"
        ),
    }


def render_r2_suite(
    markouts: list[int], cfg: FillAssumptionConfig
) -> R2SensitivityBracket:
    """Render the full R2 report-only sensitivity bracket from a pinned config.

    Uses ONLY the pinned ex-ante ``cfg`` (fields available at quote time) and
    report-only ``markouts``. No tape-reactive trigger, no depth, no queue, no
    own fills. The result is a declared model overlay -- quadruple-labeled,
    never ranked, edge/PnL tombstones ``None``.

    Args:
        markouts: Report-only markout values (declared inputs, not fills).
        cfg: Pinned, frozen ex-ante fill-assumption config.

    Returns:
        A quadruple-labeled, never-ranked ``R2SensitivityBracket``.

    Raises:
        ValueError: If ``markouts`` is empty.
    """
    if not markouts:
        raise ValueError("'markouts' must be non-empty to render an R2 suite")

    if cfg.draw_mode == "SEEDED_STOCHASTIC":
        inventory_path = _seeded_inventory_distribution(markouts, cfg)
    else:
        inventory_path = _deterministic_inventory_path(markouts, cfg)

    p = _pinned_fill_probability(cfg)
    peak = max((abs(v) for v in inventory_path.get("mean_path")
                or [inventory_path.get("expected_final", 0.0)]), default=0.0)

    pessimistic = min(markouts)
    optimistic = max(markouts)
    neutral = round(sum(markouts) / len(markouts))
    scenarios = [
        _scenario("pessimistic", pessimistic, cfg),
        _scenario("neutral", neutral, cfg),
        _scenario("optimistic", optimistic, cfg),
    ]

    return R2SensitivityBracket(
        simulated_expected_inventory_path=inventory_path,
        simulated_expected_exposure={
            "draw_mode": cfg.draw_mode,
            "model_peak_exposure": round(peak, 6),
            "note": (
                "modeled peak exposure under the pinned assumption; a declared "
                "overlay, not a measurement"
            ),
        },
        simulated_spread_capture_range={
            "pessimistic": int(round(pessimistic * p)),
            "neutral": int(round(neutral * p)),
            "optimistic": int(round(optimistic * p)),
            "note": (
                "modeled spread-capture range under the pinned ex-ante rule; a "
                "declared overlay, never a measured take"
            ),
        },
        simulated_adverse_selection_haircut={
            "model_haircut_bps": int(round(neutral * (1.0 - p))),
            "note": "modeled adverse-selection haircut under the pinned rule",
        },
        assumption_sensitivity={
            "latency_ms": cfg.latency_ms,
            "pinned_rule": cfg.fill_probability_rule,
            "rule_params": dict(cfg.rule_params),
            "draw_mode": cfg.draw_mode,
            "ex_ante_fields": list(cfg.ex_ante_fields),
            "config_hash": cfg.config_hash(),
            "scenarios": [s.model_dump() for s in scenarios],
        },
    )


def render_protection_ablation(
    markouts: list[int],
    cfg_protection_on: FillAssumptionConfig,
    cfg_protection_off: FillAssumptionConfig,
) -> R2ProtectionAblation:
    """Render a protection ON/OFF ablation -- two declared overlays, never ranked.

    Both sides are ``render_r2_suite`` outputs under different pinned
    assumptions; ``event_gate_cost`` is a MODEL delta between the two overlays,
    not a realized number.
    """
    on = render_r2_suite(markouts, cfg_protection_on)
    off = render_r2_suite(markouts, cfg_protection_off)
    on_final = on.simulated_expected_inventory_path.get(
        "expected_final", on.simulated_expected_inventory_path.get("mean_final", 0.0)
    )
    off_final = off.simulated_expected_inventory_path.get(
        "expected_final", off.simulated_expected_inventory_path.get("mean_final", 0.0)
    )
    return R2ProtectionAblation(
        protection_on=on,
        protection_off=off,
        event_gate_cost={
            "model_inventory_delta": round(off_final - on_final, 6),
            "note": (
                "modeled cost of the event gate as an overlay delta between two "
                "pinned assumptions; a declared model quantity, not a realized "
                "or measured cost"
            ),
        },
        delta_note=(
            "protection ON vs OFF is a declared model overlay comparison under "
            "pinned ex-ante assumptions; never ranked, never a fill proof"
        ),
    )
