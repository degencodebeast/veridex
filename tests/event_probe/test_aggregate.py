"""E4 aggregation / verdict tests (CON-007/009/010/011).

Covers the predeclared, fail-closed aggregate in ``aggregate.py``:

* ``_bootstrap_ci`` -- seeded percentile bootstrap of the median (deterministic).
* ``aggregate_verdict`` -- the directional CI rule, the global N_min gate, the
  per-slice N_min gate (DESCRIPTIVE_ONLY below floor), and the SPLIT-BY-SLICE
  override (two decisively-opposite slices only).

The directional set is EXACTLY the eligible events' ``R`` -- events whose
``event_class in {LAG, OVERSHOOT, REVERSAL}`` and ``R is not None``. NO-SIGNAL /
excluded records are COUNTED (``class_counts`` / ``excluded_by_reason``) but never
enter a directional CI. Records are hand-built to force one branch each.
"""

from __future__ import annotations

from veridex.backtest.event_probe.aggregate import (
    AggConfig,
    ProbeResult,
    SliceVerdict,
    _bootstrap_ci,
    aggregate_verdict,
)
from veridex.backtest.event_probe.compute import EventRecord


def _rec(
    R: float | None,
    *,
    event_class: str = "LAG",
    exclusion_reason: str | None = None,
    slice_tags: dict[str, str] | None = None,
) -> EventRecord:
    """Build an EventRecord for aggregation (only R / class / reason / tags matter).

    ``p_*`` and ``delta_*`` are dummy non-None placeholders; the aggregator reads
    only ``R``, ``event_class``, ``exclusion_reason`` and ``slice_tags``.
    """
    return EventRecord(
        t_e=1000,
        scoring_side="home",
        participant=1,
        p_pre=0.50,
        p_imm=0.55,
        p_settle=0.60,
        delta_imm=0.10,
        delta_settle=0.20,
        R=R,
        event_class=event_class,
        exclusion_reason=exclusion_reason,
        grid={},
        slice_tags=slice_tags or {},
    )


def test_bootstrap_is_seeded_deterministic() -> None:
    # Same values + same seed -> byte-identical CI across two independent calls.
    values = [0.20, 0.35, 0.40, 0.55, 0.50, 0.45, 0.60, 0.30, 0.50, 0.42]
    a = _bootstrap_ci(values, n_resamples=2000, level=0.90, seed=42)
    b = _bootstrap_ci(values, n_resamples=2000, level=0.90, seed=42)
    assert a == b
    # A real median-bootstrap of a spread sample yields a non-degenerate CI that
    # lives inside the data range -- this makes the test exercise the resampler,
    # not just its determinism.
    assert a[0] < a[1]
    assert min(values) <= a[0] <= a[1] <= max(values)


def test_global_below_nmin_is_inconclusive() -> None:
    # 29 eligible LAG events (< N_min_global=30) -> INCONCLUSIVE, never a verdict.
    records = [_rec(0.5, event_class="LAG") for _ in range(29)]
    result = aggregate_verdict(records, AggConfig())
    assert isinstance(result, ProbeResult)
    assert result.global_n == 29
    assert result.overall_verdict == "INCONCLUSIVE"
    assert result.overall_verdict not in {"FOLLOW", "FADE"}
    # Below the floor no CI is computed.
    assert result.global_ci_low is None
    assert result.global_ci_high is None


def test_ci_in_0_1_is_follow() -> None:
    # >=30 R-values tightly inside (0, 1) -> CI wholly in (0, 1) -> FOLLOW.
    records = [_rec(0.30 + 0.01 * i, event_class="LAG") for i in range(40)]
    result = aggregate_verdict(records, AggConfig())
    assert result.global_n == 40
    assert result.overall_verdict == "FOLLOW"
    assert result.global_ci_low is not None and result.global_ci_low > 0.0
    assert result.global_ci_high is not None and result.global_ci_high < 1.0


def test_ci_above_1_is_fade() -> None:
    # >=30 R-values > 1 with CI wholly above 1 -> FADE.
    records = [_rec(1.30 + 0.01 * i, event_class="OVERSHOOT") for i in range(40)]
    result = aggregate_verdict(records, AggConfig())
    assert result.global_n == 40
    assert result.overall_verdict == "FADE"
    assert result.global_ci_low is not None and result.global_ci_low > 1.0


def test_ci_straddles_is_inconclusive() -> None:
    # A wide bimodal sample: 15 at 0.8 (LAG) + 15 at 1.2 (OVERSHOOT). The median
    # bootstrap CI straddles the R=1 boundary -> INCONCLUSIVE (never forced).
    records = [_rec(0.8, event_class="LAG") for _ in range(15)]
    records += [_rec(1.2, event_class="OVERSHOOT") for _ in range(15)]
    result = aggregate_verdict(records, AggConfig())
    assert result.global_n == 30
    assert result.overall_verdict == "INCONCLUSIVE"
    # The CI genuinely spans the boundary (low below 1, high above 1).
    assert result.global_ci_low is not None and result.global_ci_low < 1.0
    assert result.global_ci_high is not None and result.global_ci_high > 1.0


def test_slice_below_15_is_descriptive_only() -> None:
    # A slice group with 14 eligible events (< N_min_slice=15) -> DESCRIPTIVE_ONLY,
    # and it cannot drive SPLIT-BY-SLICE.
    records = [
        _rec(0.5, event_class="LAG", slice_tags={"venue": "home"}) for _ in range(14)
    ]
    result = aggregate_verdict(records, AggConfig())
    home = next(s for s in result.per_slice if s.slice == "home")
    assert isinstance(home, SliceVerdict)
    assert home.n == 14
    assert home.verdict == "DESCRIPTIVE_ONLY"
    assert home.ci_low is None and home.ci_high is None
    assert result.overall_verdict != "SPLIT-BY-SLICE"


def test_split_by_slice_needs_two_opposite_decisive() -> None:
    # Two slices under one key, each clearing 15, with decisive OPPOSITE verdicts:
    # "home" all in (0,1) -> FOLLOW; "away" all > 1 -> FADE. -> SPLIT-BY-SLICE.
    split_records = [
        _rec(0.5, event_class="LAG", slice_tags={"venue": "home"}) for _ in range(15)
    ]
    split_records += [
        _rec(1.5, event_class="OVERSHOOT", slice_tags={"venue": "away"})
        for _ in range(15)
    ]
    split = aggregate_verdict(split_records, AggConfig())
    verdicts = {s.slice: s.verdict for s in split.per_slice}
    assert verdicts["home"] == "FOLLOW"
    assert verdicts["away"] == "FADE"
    assert split.overall_verdict == "SPLIT-BY-SLICE"

    # A SINGLE decisive slice (15 FOLLOW "home" + 14 descriptive "away") does NOT
    # trigger SPLIT -- one decisive slice is not a decisive disagreement.
    single_records = [
        _rec(0.5, event_class="LAG", slice_tags={"venue": "home"}) for _ in range(15)
    ]
    single_records += [
        _rec(1.5, event_class="OVERSHOOT", slice_tags={"venue": "away"})
        for _ in range(14)
    ]
    single = aggregate_verdict(single_records, AggConfig())
    assert single.overall_verdict != "SPLIT-BY-SLICE"


def test_no_signal_excluded_from_directional() -> None:
    # 30 eligible LAG (R=0.5) + 3 NO-SIGNAL records that (adversarially) carry a
    # stray non-None R=99.0. The class gate -- not "R is not None" alone -- must
    # keep the NO-SIGNAL rows OUT of the directional CI, so global_n stays 30.
    records = [_rec(0.5, event_class="LAG") for _ in range(30)]
    records += [
        _rec(99.0, event_class="NO-SIGNAL", exclusion_reason="below_epsilon")
        for _ in range(3)
    ]
    result = aggregate_verdict(records, AggConfig())

    # Directional n excludes the NO-SIGNAL rows (33 records, only 30 directional).
    assert result.global_n == 30
    assert result.overall_verdict == "FOLLOW"  # 30x 0.5 -> CI in (0,1)
    # But they ARE counted.
    assert result.class_counts["LAG"] == 30
    assert result.class_counts["NO-SIGNAL"] == 3
    assert result.excluded_by_reason["below_epsilon"] == 3


def test_split_requires_global_floor() -> None:
    # Slice dimensions OVERLAP: each event carries multiple tags, so two
    # DIFFERENT-dimension slices can each reach the slice floor (15) while the
    # DISTINCT global directional n stays below the global floor (30). SPLIT must
    # NOT override an underpowered global -- an N<30 sample fails closed to
    # INCONCLUSIVE and is never rescued into a decisive direction (CON-010
    # bullet-3, AC-005). Here: 14 venue=home (FOLLOW) + 14 half=first_half (FADE)
    # + 1 record carrying BOTH tags lifts each slice to 15 but keeps distinct
    # directional n at 29.
    records = [
        _rec(0.5, event_class="LAG", slice_tags={"venue": "home"}) for _ in range(14)
    ]
    records += [
        _rec(1.5, event_class="OVERSHOOT", slice_tags={"half": "first_half"})
        for _ in range(14)
    ]
    records.append(
        _rec(
            0.5,
            event_class="LAG",
            slice_tags={"venue": "home", "half": "first_half"},
        )
    )

    result = aggregate_verdict(records, AggConfig())

    # Both slices are individually decisive and opposite...
    venue = next(s for s in result.per_slice if s.slice == "home")
    half = next(s for s in result.per_slice if s.slice == "first_half")
    assert venue.n == 15 and venue.verdict == "FOLLOW"
    assert half.n == 15 and half.verdict == "FADE"
    # ...but the distinct global directional sample is sub-floor, so the overall
    # verdict fails closed to INCONCLUSIVE, NOT SPLIT-BY-SLICE.
    assert result.global_n == 29
    assert result.overall_verdict == "INCONCLUSIVE"
